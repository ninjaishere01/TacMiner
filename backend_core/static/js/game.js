// منع قائمة الضغط المطول على كل الصفحة
document.addEventListener('contextmenu', function(e) {
    e.preventDefault();
});


// قراءة بيانات تيليجرام الحقيقية من WebApp
const telegramWebApp = window.Telegram && window.Telegram.WebApp;
if (telegramWebApp && typeof telegramWebApp.ready === "function") {
  telegramWebApp.ready();
}

const telegramUser =
  telegramWebApp &&
  telegramWebApp.initDataUnsafe &&
  telegramWebApp.initDataUnsafe.user
    ? telegramWebApp.initDataUnsafe.user
    : null;

let telegramId = telegramUser ? telegramUser.id : null;

function resolveTelegramUsername(user) {
  if (!user) return "new user";
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ")
    .trim();
  return fullName || user.username || "new user";
}

// تهريب أي نص جاي من المستخدم قبل إدخاله في innerHTML
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

let username = resolveTelegramUsername(telegramUser);

function applyTelegramName() {
  if (!username || username === "new user") return;
  document.querySelectorAll(".my-user, #profile-username").forEach((el) => {
    el.textContent = username;
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", applyTelegramName);
} else {
  applyTelegramName();
}

// referral_id من WebApp.initDataUnsafe.start_param فقط، ولو القيمة تبدأ بـ ref_
function captureReferralId() {
  const webApp = window.Telegram && window.Telegram.WebApp;
  const startParam =
    webApp && webApp.initDataUnsafe ? webApp.initDataUnsafe.start_param : null;
  if (startParam == null || startParam === "") return null;
  const value = String(startParam).trim();
  if (!value.toLowerCase().startsWith("ref_")) return null;
  const referrerId = value.slice(4).trim();
  return /^\d+$/.test(referrerId) ? referrerId : null;
}

let referralId = captureReferralId();

document.addEventListener("DOMContentLoaded", () => {
  const latestReferralId = captureReferralId();
  if (latestReferralId) referralId = latestReferralId;
});

const allWorkers = [
  {
    id: 1,
    name: "Alpha",
    icon: "fa-person-digging",
    image: "/static/images/1.png",
    basePrice: 100,
  },
  {
    id: 2,
    name: "Dragon",
    icon: "fa-dragon",
    image: "/static/images/2.png",
    basePrice: 250,
  },
  {
    id: 3,
    name: "Hawk",
    icon: "fa-crow",
    image: "/static/images/3.png",
    basePrice: 500,
  },
  {
    id: 4,
    name: "Killer",
    icon: "fa-user-ninja",
    image: "/static/images/4.png",
    basePrice: 1000,
  },
  {
    id: 5,
    name: "Puglist",
    icon: "fa-hand-fist",
    image: "/static/images/5.png",
    basePrice: 2500,
  },
  {
    id: 6,
    name: "Shooter",
    icon: "fa-gun",
    image: "/static/images/6.png",
    basePrice: 5000,
  },
  {
    id: 7,
    name: "Romeo",
    icon: "fa-user-tie",
    image: "/static/images/7.png",
    basePrice: 10000,
  },
  {
    id: 8,
    name: "Detector",
    icon: "fa-magnifying-glass",
    image: "/static/images/8.png",
    basePrice: 25000,
  },
];

function getPlayerData() {
  let data = localStorage.getItem("minerPlayerData");
  if (!data) {
    data = { balance: 1500, workers: {} };
    localStorage.setItem("minerPlayerData", JSON.stringify(data));
  } else {
    data = JSON.parse(data);
  }
  return data;
}

function savePlayerData(data) {
  localStorage.setItem("minerPlayerData", JSON.stringify(data));
  if (typeof updateBalanceUI === "function") updateBalanceUI();
}

function loadAdsgramSdk() {
  if (window.Adsgram) return Promise.resolve(window.Adsgram);

  return new Promise((resolve, reject) => {
    const onLoad = () => {
      if (window.Adsgram) resolve(window.Adsgram);
      else reject(new Error("AdsGram SDK unavailable"));
    };
    const existing = document.querySelector('script[data-adsgram-sdk="1"]');
    if (existing) {
      existing.addEventListener("load", onLoad);
      existing.addEventListener("error", () =>
        reject(new Error("AdsGram SDK failed to load")),
      );
      return;
    }

    const script = document.createElement("script");
    script.src = "https://sad.adsgram.ai/js/sad.min.js";
    script.async = true;
    script.dataset.adsgramSdk = "1";
    script.addEventListener("load", onLoad);
    script.addEventListener("error", () =>
      reject(new Error("AdsGram SDK failed to load")),
    );
    document.head.appendChild(script);
  });
}

async function showRewardedAd(blockId) {
  const Adsgram = await loadAdsgramSdk();
  if (!Adsgram || typeof Adsgram.init !== "function") {
    throw new Error("AdsGram init unavailable");
  }

  const controller = Adsgram.init({ blockId });
  if (!controller || typeof controller.show !== "function") {
    throw new Error("AdsGram show unavailable");
  }

  await controller.show();
}

async function buyWorker(workerId, price) {
  const initData =
    window.Telegram && window.Telegram.WebApp
      ? window.Telegram.WebApp.initData
      : "";

  if (!initData) {
    Swal.fire({
      title: "افتح التطبيق من تيليجرام",
      text: "لازم تفتح اللعبة من داخل بوت تيليجرام.",
      icon: "info",
    });
    return;
  }

  try {
    const startRes = await fetch("/api/ads/start/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData, worker_id: workerId }),
    });
    const start = await startRes.json().catch(() => ({}));

    if (!startRes.ok) {
      Swal.fire({
        title: "خطأ",
        text: start.message || "تعذر تجهيز العملية، حاول تاني.",
        icon: "error",
      });
      return;
    }

    let adSession = null;
    if (start.enabled) {
      try {
        await showRewardedAd(start.block_id);
      } catch (err) {
        Swal.fire({
          title: "الإعلان لم يكتمل",
          text: "لازم تشاهد الإعلان للآخر عشان تقدر تكمل.",
          icon: "info",
        });
        return;
      }

      adSession = start.session;
      Swal.fire({
        title: "جاري تأكيد الإعلان...",
        allowOutsideClick: false,
        allowEscapeKey: false,
        didOpen: () => Swal.showLoading(),
      });
    }

    let response = null;
    for (let attempt = 0; attempt < 10; attempt++) {
      const res = await fetch("/api/upgrade/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initData,
          worker_id: workerId,
          ad_session: adSession,
        }),
      });
      const data = await res.json().catch(() => ({}));
      response = { status: res.status, data };
      if (res.status !== 409) break;
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }

    Swal.close();

    if (response.status === 200) {
      const data = response.data;
      updateBalanceUI(data.new_balance);
      updateUpgradeUI();

      if (data.production) {
        if (typeof window.applyProduction === "function") {
          window.applyProduction(data.production);
        } else {
          window.productionPerSecond =
            Number(data.production.per_hour) / 3600;
        }
      }

      Swal.fire({
        title: "تمت الترقية!",
        text: `تم ترقية العامل للمستوى ${data.new_level}.`,
        icon: "success",
        timer: 3500,
        showConfirmButton: false,
      });
      return;
    }

    const errorCode = response.data.error;
    const messages = {
      ad_pending: "الإعلان اتأخر في التأكيد. استنى لحظة وجرّب تاني.",
      ad_required: "لازم تشاهد الإعلان للآخر قبل الشراء أو الترقية.",
      "Insufficient balance": "رصيدك مش كفاية لإتمام العملية.",
      Unauthorized: "افتح التطبيق من تيليجرام وجرّب تاني.",
    };

    Swal.fire({
      title: "مش ممكن",
      text: messages[errorCode] || "حصل خطأ، حاول تاني.",
      icon: "error",
    });
  } catch (err) {
    Swal.close();
    Swal.fire({
      title: "خطأ في الاتصال",
      text: "حاول تاني بعد لحظات.",
      icon: "error",
    });
  }
}
