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

function readTelegramStartParam() {
  const unsafeParam =
    telegramWebApp &&
    telegramWebApp.initDataUnsafe &&
    telegramWebApp.initDataUnsafe.start_param;
  if (unsafeParam) return String(unsafeParam);

  const sources = [];
  if (window.location.hash) sources.push(window.location.hash.slice(1));
  if (window.location.search) sources.push(window.location.search.slice(1));
  for (const source of sources) {
    const params = new URLSearchParams(source);
    const value =
      params.get("tgWebAppStartParam") ||
      params.get("startapp") ||
      params.get("start_param");
    if (value) return value;
  }
  return "";
}

// referral_id جاي من start_param (ref_<telegram_id>)
let referralId = null;
const startParam = readTelegramStartParam();
if (startParam) {
  const normalized = String(startParam).startsWith("ref_")
    ? String(startParam).slice(4)
    : String(startParam);
  if (/^\d+$/.test(normalized)) referralId = normalized;
}

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

function buyWorker(workerId, price) {
  fetch("/api/upgrade/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ telegram_id: telegramId, worker_id: workerId }),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.message === "Upgraded!") {
        updateBalanceUI(data.new_balance);
        updateUpgradeUI();

        if (data.production) {
          if (typeof window.applyProduction === "function") {
            window.applyProduction(data.production);
          } else {
            window.productionPerSecond = Number(data.production.per_hour) / 3600;
          }
        }

        Swal.fire({
          title: "تمت الترقية!",
          text: `تم ترقية العامل للمستوى ${data.new_level}.`,
          icon: "success",
          timer: 3500,
          showConfirmButton: false,
        });
      }
    });
}
