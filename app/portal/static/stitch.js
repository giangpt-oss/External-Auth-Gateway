const context = JSON.parse(document.getElementById("stitch-context").textContent);

function feedback(message, error = false) {
  let notice = document.querySelector("[data-stitch-feedback]");
  if (!notice) {
    notice = document.createElement("p");
    notice.dataset.stitchFeedback = "";
    document.querySelector("main").prepend(notice);
  }
  notice.textContent = message;
  notice.toggleAttribute("data-error", error);
  notice.setAttribute("role", error ? "alert" : "status");
}

function submitAction(action, values = {}) {
  const form = document.createElement("form");
  form.method = "post";
  form.action = action;
  for (const [name, value] of Object.entries({ csrf_token: context.csrf, ...values })) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
}

for (const button of document.querySelectorAll("button[data-stitch-theme]")) {
  button.addEventListener("click", () => {
    const theme = button.dataset.stitchTheme;
    try { localStorage.setItem("gateway_theme", theme); } catch {}
    const url = new URL(location.href);
    if (url.pathname === "/auth/login") url.pathname = "/login";
    url.searchParams.set("theme", theme);
    location.assign(url);
  });
}

const password = document.getElementById("odoo_password");
document.getElementById("togglePasswordBtn")?.addEventListener("click", event => {
  const reveal = password.type === "password";
  password.type = reveal ? "text" : "password";
  document.getElementById("pwdToggleIcon").textContent = reveal ? "visibility_off" : "visibility";
  event.currentTarget.setAttribute("aria-pressed", String(reveal));
  event.currentTarget.setAttribute("aria-label", reveal ? "Ẩn mật khẩu" : "Hiện mật khẩu");
});
document.getElementById("odooLoginForm")?.addEventListener("submit", () => {
  document.getElementById("btnLabel").textContent = "Đang xác thực…";
  document.getElementById("btnIcon").classList.add("hidden");
  document.getElementById("btnSpinner").classList.remove("hidden");
  document.getElementById("submitBtn").disabled = true;
});

const search = document.querySelector("[data-stitch-search]");
function filterAccounts() {
  const query = search.value.trim().toLowerCase();
  for (const row of document.querySelectorAll("[data-stitch-accounts] > [data-account-email]")) {
    row.hidden = !row.dataset.accountEmail.toLowerCase().includes(query);
  }
}
search?.addEventListener("input", filterAccounts);
document.querySelector("[data-stitch-search-button]")?.addEventListener("click", filterAccounts);

for (const button of document.querySelectorAll('[data-stitch-action="activate"]')) {
  if (button.dataset.accountStatus !== "connected") {
    button.disabled = true;
    button.textContent = "Cần kết nối lại";
    button.title = "Tài khoản Google cần được kết nối lại";
  }
}
const isDarkTheme = document.documentElement.classList.contains("dark") ||
  document.documentElement.dataset.stitchTheme === "dark" ||
  new URLSearchParams(location.search).get("theme") === "dark";
for (const app of document.querySelectorAll("[data-stitch-app]")) {
  const name = app.dataset.stitchApp;
  const enabled = Boolean(context.workspaceApps[name]);
  app.dataset.enabled = String(enabled);
  app.setAttribute("aria-label", name + (enabled ? " · Đã cấp quyền" : " · Chưa cấp quyền"));

  const label = app.querySelector("span.truncate");
  const dot = app.lastElementChild;
  const iconBox = app.firstElementChild?.firstElementChild;
  const icon = iconBox?.querySelector(".material-symbols-outlined");

  if (enabled) {
    app.classList.remove("border-dashed", "opacity-75", "opacity-70", "opacity-60", "bg-slate-50", "bg-slate-50/60");
    app.style.borderStyle = "solid";
    app.style.opacity = "1";
    app.style.backgroundColor = isDarkTheme ? "#051424" : "#ffffff";
    app.style.borderColor = isDarkTheme ? "#1c2d42" : "#e2e8f0";
    if (label) {
      label.classList.remove("text-slate-500", "text-slate-400", "text-slate-600");
      label.style.color = isDarkTheme ? "#e2e8f0" : "#1e293b";
    }
    if (dot) {
      dot.classList.remove("bg-slate-300", "bg-slate-600");
      dot.style.backgroundColor = isDarkTheme ? "#2dd4bf" : "#10b981";
      dot.style.boxShadow = isDarkTheme ? "0 0 8px #2dd4bf" : "0 1px 2px rgba(0, 0, 0, 0.05)";
      dot.title = "Đã kết nối";
    }
    if (name === "calendar" && iconBox && icon) {
      iconBox.classList.remove("bg-slate-100", "bg-[#0d1c2d]");
      iconBox.style.backgroundColor = isDarkTheme ? "rgba(59, 130, 246, 0.16)" : "#eff6ff";
      icon.classList.remove("text-slate-400", "text-slate-500");
      icon.style.color = isDarkTheme ? "#60a5fa" : "#2563eb";
    }
  } else {
    app.classList.add("border-dashed");
    app.style.borderStyle = "dashed";
    app.style.opacity = isDarkTheme ? "0.6" : "0.75";
    app.style.backgroundColor = isDarkTheme ? "#051424" : "#f8fafc";
    app.style.borderColor = isDarkTheme ? "#1c2d42" : "#cbd5e1";
    if (label) {
      label.style.color = isDarkTheme ? "#94a3b8" : "#64748b";
    }
    if (dot) {
      dot.classList.remove("bg-teal-500", "bg-emerald-500", "shadow-sm", "bg-[#2dd4bf]", "shadow-[0_0_8px_#2dd4bf]");
      dot.style.backgroundColor = isDarkTheme ? "#475569" : "#cbd5e1";
      dot.style.boxShadow = "none";
      dot.title = "Chưa kết nối";
    }
    if (name === "calendar" && iconBox && icon) {
      iconBox.style.backgroundColor = isDarkTheme ? "#0d1c2d" : "#f1f5f9";
      icon.style.color = "#94a3b8";
    }
  }
}
if (context.error && !document.getElementById("odooLoginForm")) feedback(context.error, true);
if (context.draining) feedback("Đang chờ tác vụ hoàn tất. Chọn lại tài khoản khi các tác vụ đã kết thúc.");

let telegramBusy = false;
let telegramTimer;
let telegramDeadline = 0;
let telegramVerifyButton = null;
let telegramVerifyLink = null;
async function pollTelegram() {
  if (Date.now() >= telegramDeadline) {
    if (telegramVerifyLink && telegramVerifyButton) {
      telegramVerifyLink.replaceWith(telegramVerifyButton);
      telegramVerifyLink = null;
    }
    feedback("Mã QR đã hết hạn. Hãy tạo mã xác minh mới.", true);
    return;
  }
  try {
    const response = await fetch("/connections/telegram/verification-status", { cache: "no-store" });
    if (response.status === 401) {
      feedback("Phiên Odoo đã hết hạn. Vui lòng đăng nhập lại.", true);
      return;
    }
    if (response.ok) {
      const status = await response.json();
      if (status.verified && status.revision !== context.telegramRevision) {
        location.reload();
        return;
      }
    }
  } catch {
    // Retry transient failures while the issued verification link is valid.
  }
  telegramTimer = setTimeout(pollTelegram, 2000);
}

async function verifyTelegram(button, { silent = false } = {}) {
  if (telegramBusy) return;
  if (!context.telegramConfigured) {
    if (!silent) feedback("Gateway cần được cấu hình xác minh Telegram để tạo mã QR.", true);
    return;
  }
  telegramBusy = true;
  clearTimeout(telegramTimer);
  const originalLabel = button ? button.textContent : "";
  if (button) {
    telegramVerifyButton = button;
    button.setAttribute("aria-busy", "true");
  }
  try {
    const response = await fetch("/connections/telegram/verify", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": context.csrf,
      },
      body: new URLSearchParams({ csrf_token: context.csrf }),
      redirect: "error",
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Không tạo được mã QR. Vui lòng thử lại.");
    const data = await response.json();
    const url = new URL(data.link_url);
    const expiresAt = Date.parse(data.expires_at);
    if (url.protocol !== "https:" || !Number.isFinite(expiresAt) ||
        !data.qr_code_data_uri?.startsWith("data:image/png;base64,")) {
      throw new Error("Mã xác minh trả về không hợp lệ.");
    }
    const placeholder = document.querySelector("[data-stitch-qr]");
    if (placeholder) {
      const image = document.createElement("img");
      image.src = data.qr_code_data_uri;
      image.alt = "Mã QR xác minh tài khoản Telegram";
      image.className = typeof placeholder.className === "string"
        ? placeholder.className
        : (placeholder.className.baseVal || "w-32 h-32 object-contain");
      image.setAttribute("data-stitch-qr", "");
      placeholder.replaceWith(image);
    }
    if (button) {
      const link = document.createElement("a");
      link.className = button.className;
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Mở Telegram để xác minh";
      button.replaceWith(link);
      telegramVerifyLink = link;
    }
    telegramDeadline = expiresAt;
    telegramTimer = setTimeout(pollTelegram, 2000);
    if (!silent) feedback("Quét mã QR hoặc mở Telegram để xác minh tài khoản.");
  } catch (error) {
    if (button) button.textContent = originalLabel;
    if (!silent) feedback(error.message, true);
  } finally {
    telegramBusy = false;
    if (button) button.removeAttribute("aria-busy");
  }
}

// Automatically load the real Telegram verification QR code on dashboard open
const initialQr = document.querySelector("[data-stitch-qr]");
const initialVerifyBtn = document.querySelector('[data-stitch-action="verify-telegram"]');
if (initialQr && context.telegramConfigured && context.telegramState !== "verified") {
  verifyTelegram(initialVerifyBtn, { silent: true });
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-stitch-action]");
  if (!button) return;
  event.preventDefault();
  const action = button.dataset.stitchAction;
  if (action === "logout") submitAction("/auth/logout");
  if (action === "add") submitAction("/connections/google/start", { mode: "add" });
  if (action === "activate") {
    const step = context.draining ? "confirm" : "activate";
    submitAction(`/connections/google/accounts/${button.dataset.accountId}/${step}?current_version=${context.selectionVersion}`);
  }
  if (action === "delete" && window.confirm(`Xóa tài khoản Google ${button.dataset.accountEmail}?`)) {
    submitAction(`/connections/google/accounts/${button.dataset.accountId}/delete`);
  }
  if (action === "disconnect-telegram" && window.confirm("Ngắt kết nối Telegram?")) {
    submitAction("/connections/telegram/disconnect");
  }
  if (action === "verify-telegram") verifyTelegram(button);
});
