for (const choice of document.querySelectorAll('input[name="portal-theme"]')) {
  choice.checked = choice.value === document.documentElement.dataset.theme;
  choice.addEventListener("change", () => {
    document.documentElement.dataset.theme = choice.value;
    try { localStorage.setItem("gateway_theme", choice.value); } catch {}
  });
}

const accounts = document.getElementById("account-picker");
accounts?.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !accounts.open) return;
  const management = accounts.querySelector(".account-management");
  if (management.open && management.contains(event.target)) {
    management.open = false;
    management.querySelector("summary").focus();
  } else {
    accounts.open = false;
    accounts.querySelector("summary").focus();
  }
  event.preventDefault();
});

document.addEventListener("submit", (event) => {
  const { deleteEmail, confirm: confirmation } = event.target.dataset;
  if (deleteEmail && window.prompt(`Nhập đúng email để xóa tài khoản ${deleteEmail}.`) !== deleteEmail) {
    event.preventDefault();
  }
  if (confirmation && !window.confirm(confirmation)) event.preventDefault();
});

const telegram = document.querySelector("[data-telegram-state]");
if (telegram) {
  const form = telegram.querySelector("[data-telegram-verify]");
  const button = form?.querySelector("button") || telegram.querySelector("[data-telegram-start]");
  const linkBox = telegram.querySelector("[data-telegram-link]");
  const progress = telegram.querySelector("[data-telegram-progress]");
  const qr = linkBox.querySelector("img");
  const openLink = linkBox.querySelector("a");
  let deadline = 0;
  let timer;
  let checking = false;
  let issuing = false;

  function showProgress(message) {
    progress.hidden = false;
    progress.textContent = message;
  }

  function clearLink() {
    deadline = 0;
    clearTimeout(timer);
    linkBox.hidden = true;
    qr.removeAttribute("src");
    openLink.removeAttribute("href");
  }

  async function poll() {
    if (!deadline || checking || issuing) return;
    if (Date.now() >= deadline) {
      clearLink();
      showProgress("Mã QR đã hết hạn. Hãy tạo mã mới.");
      return;
    }

    checking = true;
    const attemptedDeadline = deadline;
    try {
      const response = await fetch("/connections/telegram/verification-status", { cache: "no-store" });
      if (attemptedDeadline !== deadline) return;
      if (response.status === 401) {
        clearLink();
        showProgress("Phiên Odoo đã hết hạn. Vui lòng đăng nhập lại.");
        return;
      }
      if (!response.ok) return;
      const result = await response.json();
      if (attemptedDeadline !== deadline) return;
      const changed = result.revision !== telegram.dataset.telegramRevision;
      if (changed && (form ? result.verified : result.linked)) {
        clearLink();
        location.replace(form ? "/?status=telegram_verified" : "/");
      }
    } catch {
      // Retry transient network failures until the link expires.
    } finally {
      checking = false;
      if (deadline && !issuing) timer = setTimeout(poll, 2000);
    }
  }

  async function issue(event) {
    event.preventDefault();
    if (issuing) return;
    issuing = true;
    button.disabled = true;
    clearLink();
    showProgress("Đang tạo mã QR…");
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]').content;
      const response = await fetch(form?.action || button.dataset.telegramStart, {
        method: "POST",
        headers: { Accept: "application/json", "X-CSRF-Token": csrf },
        body: form ? new FormData(form) : undefined,
        redirect: "error",
        cache: "no-store",
      });
      if (!response.ok) {
        if (response.status === 401) showProgress("Phiên Odoo đã hết hạn. Vui lòng đăng nhập lại.");
        else showProgress("Không tạo được mã QR. Vui lòng thử lại.");
        return;
      }

      const data = await response.json();
      const link = new URL(data.link_url);
      const expiry = Date.parse(data.expires_at);
      if (link.protocol !== "https:" || !Number.isFinite(expiry) ||
          !data.qr_code_data_uri.startsWith("data:image/png;base64,")) {
        throw new Error("Invalid Telegram link");
      }
      qr.src = data.qr_code_data_uri;
      openLink.href = link.href;
      deadline = expiry;
      linkBox.hidden = false;
      button.textContent = "Tạo mã mới";
      showProgress(form ? "Đang chờ xác minh từ Gateway…" : "Đang chờ liên kết Telegram…");
      openLink.focus();
    } catch {
      showProgress("Không tạo được mã QR. Vui lòng thử lại.");
    } finally {
      issuing = false;
      button.disabled = false;
      if (deadline) timer = setTimeout(poll, 2000);
    }
  }

  if (form) form.addEventListener("submit", issue);
  else button?.addEventListener("click", issue);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && deadline) {
      clearTimeout(timer);
      poll();
    }
  });
}

const accountSearch = document.getElementById("account-search");
function filterAccounts() {
  const query = accountSearch.value.trim().toLowerCase();
  document.querySelectorAll("[data-account-email]").forEach(row => {
    row.hidden = !row.dataset.accountEmail.toLowerCase().includes(query);
  });
}
accountSearch?.addEventListener("input", filterAccounts);
document.getElementById("find-account")?.addEventListener("click", filterAccounts);

document.getElementById("show-password")?.addEventListener("click", event => {
  const field = document.getElementById("secret");
  const reveal = field.type === "password";
  field.type = reveal ? "text" : "password";
  event.currentTarget.setAttribute("aria-pressed", String(reveal));
  event.currentTarget.setAttribute("aria-label", reveal ? "Ẩn mật khẩu" : "Hiện mật khẩu");
});
