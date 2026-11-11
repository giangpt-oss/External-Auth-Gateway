// Client side helper for Telegram QR code linking
document.addEventListener("DOMContentLoaded", () => {
  const verificationOptions = document.getElementById("telegram-verification-options");
  const verificationProgress = document.getElementById("telegram-verification-progress");

  if (verificationOptions && verificationProgress) {
    const initialRevision = verificationOptions.dataset.telegramRevision;
    const initialState = verificationOptions.dataset.telegramState;
    let verificationDeadline = 0;
    let pollTimer;
    let checking = false;

    async function checkVerification() {
      if (checking) return;
      checking = true;
      clearTimeout(pollTimer);
      let keepPolling = true;

      try {
        const response = await fetch("/connections/telegram/verification-status", { cache: "no-store" });
        if (response.status === 401) {
          verificationProgress.hidden = false;
          verificationProgress.textContent = "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.";
          keepPolling = false;
          return;
        }
        if (response.ok) {
          const result = await response.json();
          if (result.verified && result.revision !== initialRevision && verificationDeadline) {
            keepPolling = false;
            window.location.replace("/?status=telegram_verified");
            return;
          }
          if (result.state !== initialState && !verificationDeadline) {
            keepPolling = false;
            window.location.replace("/");
            return;
          }
        }
      } catch {
        // Retry on a temporary network error.
      } finally {
        checking = false;
        if (verificationDeadline && Date.now() >= verificationDeadline) {
          verificationDeadline = 0;
          verificationProgress.textContent = "Chưa nhận được xác minh thành công. Bạn có thể thử lại.";
        }
        if (keepPolling && (verificationDeadline || !document.hidden)) {
          pollTimer = setTimeout(checkVerification, verificationDeadline ? 2000 : 15000);
        }
      }
    }

    verificationOptions.addEventListener("submit", () => {
      verificationDeadline = Date.now() + 5 * 60 * 1000;
      verificationProgress.hidden = false;
      verificationProgress.textContent = "Đang chờ kết quả từ Telegram…";
      clearTimeout(pollTimer);
      pollTimer = setTimeout(checkVerification, 2000);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) checkVerification();
    });
    if (!document.hidden) pollTimer = setTimeout(checkVerification, 15000);
  }

  const linkTgBtn = document.getElementById("btn-link-telegram");
  const tgContainer = document.getElementById("telegram-link-box");

  if (linkTgBtn && tgContainer) {
    linkTgBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      linkTgBtn.disabled = true;
      linkTgBtn.innerText = "Đang tạo liên kết...";

      try {
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
        const res = await fetch("/connections/telegram/link", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
        });

        if (!res.ok) {
          const err = await res.json();
          alert("Lỗi tạo liên kết Telegram: " + (err.error?.message || "Vui lòng thử lại"));
          linkTgBtn.disabled = false;
          linkTgBtn.innerText = "Tạo mã QR / Link";
          return;
        }

        const data = await res.json();
        tgContainer.innerHTML = `
          <div class="qr-container">
            <p><strong>1. Quét mã QR bằng camera/Telegram:</strong></p>
            <img src="${data.qr_code_data_uri}" alt="Telegram QR Link" />
            <p><strong>2. Hoặc nhấp vào link trực tiếp:</strong></p>
            <a href="${data.link_url}" target="_blank" rel="noopener" class="btn btn-primary">Mở Telegram Bot</a>
            <p class="help-text" id="tg-countdown">Mã liên kết có hiệu lực trong: 10 phút 00 giây</p>
          </div>
        `;
        tgContainer.style.display = "block";

        // Countdown timer 10 mins (600s)
        let timeLeft = 600;
        const countdownElem = document.getElementById("tg-countdown");
        const timer = setInterval(() => {
          timeLeft--;
          if (timeLeft <= 0) {
            clearInterval(timer);
            if (countdownElem) countdownElem.innerText = "Mã liên kết đã hết hạn. Vui lòng tạo lại!";
          } else {
            const mins = Math.floor(timeLeft / 60);
            const secs = timeLeft % 60;
            if (countdownElem) {
              countdownElem.innerText = `Mã liên kết có hiệu lực trong: ${mins} phút ${secs.toString().padStart(2, '0')} giây`;
            }
          }
        }, 1000);

      } catch (err) {
        alert("Có lỗi mạng xảy ra: " + err);
        linkTgBtn.disabled = false;
        linkTgBtn.innerText = "Tạo mã QR / Link";
      }
    });
  }
});
