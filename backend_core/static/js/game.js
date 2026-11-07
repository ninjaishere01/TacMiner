// قراءة بيانات تيليجرام
let telegramUser = window.Telegram?.WebApp?.initDataUnsafe?.user;
let telegramId = telegramUser ? telegramUser.id : 123456789;
let username = telegramUser
  ? telegramUser.username || telegramUser.first_name
  : "newuser";

// قراءة رابط الإحالة (لو موجود)
let referralId = null;
let startParam = window.Telegram?.WebApp?.initDataUnsafe?.start_param;
if (startParam && startParam.startsWith("ref_")) {
  referralId = startParam.replace("ref_", "");
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

        // تحديث الإنتاج
        if (data.production) {
          window.productionPerSecond = data.production.per_hour / 3600;
          // تحديث الإحصائيات
          document.getElementById("stat-hour").innerText =
            data.production.per_hour.toFixed(2);
          document.getElementById("stat-day").innerText =
            data.production.per_day.toFixed(2);
          document.getElementById("stat-month").innerText =
            data.production.per_month.toFixed(2);
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
