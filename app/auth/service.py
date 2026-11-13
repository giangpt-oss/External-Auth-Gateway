from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from app.models import OdooCredential, Session, User
from app.odoo.client import OdooAccessError, OdooClient
from app.security import decrypt_secret, encrypt_secret, generate_secure_token, hash_token


@dataclass
class AuthenticatedUser:
    id: int
    tenant: str
    database: str
    odoo_uid: int
    login: str
    name: str
    partner_id: int | None
    tz: str
    active: bool


class AuthError(Exception):
    pass


class RateLimitExceeded(AuthError):
    pass


class InactiveUserError(AuthError):
    pass


class PortalUserError(AuthError):
    pass


class InvalidSessionError(AuthError):
    pass


class AuthService:
    def __init__(
        self,
        session_factory: sessionmaker,
        odoo_client: OdooClient,
        tenant: str,
        database: str,
        encryption_key: str,
        session_lifetime_hours: int = 8,
    ):
        self.session_factory = session_factory
        self.odoo_client = odoo_client
        self.tenant = tenant
        self.database = database
        self.encryption_key = encryption_key
        self.session_lifetime_hours = session_lifetime_hours

        # Rate limiting: (ip, normalized_login) -> list of failure timestamps
        self._failed_attempts: dict[tuple[str, str], list[datetime]] = {}
        # Credential validation cache: (user_id, credential_hash) -> last_verified_datetime
        self._credential_cache: dict[tuple[int, str], datetime] = {}

    def _check_rate_limit(self, ip: str, login: str) -> None:
        key = (ip, login.strip().lower())
        now = datetime.now(UTC)
        window_start = now - timedelta(minutes=15)

        attempts = self._failed_attempts.get(key, [])
        # Filter attempts within the 15-minute window
        valid_attempts = [t for t in attempts if t > window_start]
        self._failed_attempts[key] = valid_attempts

        if len(valid_attempts) >= 5:
            raise RateLimitExceeded(
                "Too many failed login attempts. Please try again after 15 minutes."
            )

    def _record_failed_attempt(self, ip: str, login: str) -> None:
        key = (ip, login.strip().lower())
        now = datetime.now(UTC)
        if key not in self._failed_attempts:
            self._failed_attempts[key] = []
        self._failed_attempts[key].append(now)

    def _clear_failed_attempts(self, ip: str, login: str) -> None:
        key = (ip, login.strip().lower())
        self._failed_attempts.pop(key, None)

    def login(
        self,
        login: str,
        secret: str,
        credential_kind: str = "password",
        client_ip: str = "127.0.0.1",
    ) -> tuple[AuthenticatedUser, str, str]:
        login = login.strip()
        self._check_rate_limit(client_ip, login)

        try:
            if credential_kind != "password":
                raise AuthError("Odoo account password is required.")
            uid = self.odoo_client.authenticate_password(login, secret)
        except Exception:
            self._record_failed_attempt(client_ip, login)
            raise

        # Fetch profile
        try:
            fields = ["id", "name", "login", "active", "share", "tz", "partner_id"]
            records = self.odoo_client.execute(
                uid=uid,
                secret=secret,
                model="res.users",
                method="read",
                args=[[uid]],
                kwargs={"fields": fields},
            )
            if not records or not isinstance(records, list):
                self._record_failed_attempt(client_ip, login)
                raise AuthError("Failed to fetch user profile from Odoo.")
            profile = records[0]
        except OdooAccessError as exc:
            self._record_failed_attempt(client_ip, login)
            raise AuthError("Odoo denied access to the user profile.") from exc
        except Exception:
            self._record_failed_attempt(client_ip, login)
            raise

        if not profile.get("active", True):
            self._record_failed_attempt(client_ip, login)
            raise InactiveUserError(
                "Your Odoo user account is inactive. Please contact administrator."
            )

        if profile.get("share", False):
            self._record_failed_attempt(client_ip, login)
            raise PortalUserError("Portal/external users are not allowed to access this portal.")

        # Success - clear rate limit
        self._clear_failed_attempts(client_ip, login)

        # Parse partner_id (may be [id, "Name"] or False)
        partner_val = profile.get("partner_id")
        partner_id = partner_val[0] if isinstance(partner_val, list | tuple) else partner_val

        # Upsert user and encrypted credential in DB
        with self.session_factory() as db:
            user = (
                db.query(User)
                .filter_by(tenant=self.tenant, database=self.database, odoo_uid=uid)
                .first()
            )
            if not user:
                user = User(
                    tenant=self.tenant,
                    database=self.database,
                    odoo_uid=uid,
                    login=profile.get("login", login),
                    name=profile.get("name", login),
                    partner_id=partner_id,
                    tz=profile.get("tz") or "Asia/Bangkok",
                    active=True,
                )
                db.add(user)
                db.flush()
            else:
                user.login = profile.get("login", login)
                user.name = profile.get("name", login)
                user.partner_id = partner_id
                user.tz = profile.get("tz") or user.tz
                user.active = True

            # Encrypt credential
            enc_secret = encrypt_secret(secret, self.encryption_key)
            cred = db.query(OdooCredential).filter_by(user_id=user.id).first()
            if not cred:
                cred = OdooCredential(
                    user_id=user.id,
                    credential_kind=credential_kind,
                    encrypted_secret=enc_secret,
                )
                db.add(cred)
            else:
                cred.credential_kind = credential_kind
                cred.encrypted_secret = enc_secret

            # Invalidate old sessions for this user
            db.query(Session).filter_by(user_id=user.id).delete()

            # Create new session
            session_token = generate_secure_token(32)
            token_hash = hash_token(session_token)
            csrf_secret = generate_secure_token(32)
            now = datetime.now(UTC)
            expires_at = now + timedelta(hours=self.session_lifetime_hours)

            session_record = Session(
                token_hash=token_hash,
                user_id=user.id,
                csrf_secret=csrf_secret,
                expires_at=expires_at,
            )
            db.add(session_record)
            db.commit()

            auth_user = AuthenticatedUser(
                id=user.id,
                tenant=user.tenant,
                database=user.database,
                odoo_uid=user.odoo_uid,
                login=user.login,
                name=user.name,
                partner_id=user.partner_id,
                tz=user.tz,
                active=user.active,
            )

            return auth_user, session_token, csrf_secret

    def get_user_by_session(self, session_token: str) -> tuple[AuthenticatedUser, Session] | None:
        if not session_token:
            return None
        token_hash = hash_token(session_token)
        now = datetime.now(UTC)

        with self.session_factory() as db:
            session_rec = (
                db.query(Session)
                .filter(Session.token_hash == token_hash, Session.expires_at > now)
                .first()
            )
            if not session_rec:
                return None

            user = db.query(User).filter_by(id=session_rec.user_id).first()
            if not user or not user.active:
                return None

            auth_user = AuthenticatedUser(
                id=user.id,
                tenant=user.tenant,
                database=user.database,
                odoo_uid=user.odoo_uid,
                login=user.login,
                name=user.name,
                partner_id=user.partner_id,
                tz=user.tz,
                active=user.active,
            )
            return auth_user, session_rec

    def logout(self, session_token: str) -> None:
        if not session_token:
            return
        token_hash = hash_token(session_token)
        with self.session_factory() as db:
            db.query(Session).filter_by(token_hash=token_hash).delete()
            db.commit()

    def get_user_secret(self, user_id: int) -> str | None:
        with self.session_factory() as db:
            cred = db.query(OdooCredential).filter_by(user_id=user_id).first()
            if cred and cred.encrypted_secret:
                return decrypt_secret(cred.encrypted_secret, self.encryption_key)
            return None
