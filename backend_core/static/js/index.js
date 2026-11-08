// 1. تحديث الرصيد
function updateBalanceUI(balance) {
  if (balance === undefined || balance === null || balance === "") return;
  const numeric = Number(balance);
  if (!Number.isFinite(numeric)) return;

  const formatted = numeric.toLocaleString("en-US");
  document.querySelectorAll("#global-balance, #profile-balance").forEach((el) => {
    el.innerText = formatted;
  });
}

// 2. رسم الصفحة الرئيسية (Home)
function renderHomeGrid() {
  const container = document.getElementById("mine-grid");
  if (!container) return;

  // بنجيب العمال من الـ API
  fetch("/api/workers/")
    .then((res) => res.json())
    .then((workers) => {
      // بنجيب بيانات اليوزر (اللي فيها العمال اللي معاه)
      fetch("/api/login/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          telegram_id: telegramId,
          username: username,
          referral_id: referralId,
        }),
      })
        .then((res) => res.json())
        .then((userData) => {
          const userWorkers = userData.user.workers;
          let html = "";

          workers.forEach((worker) => {
            const level = userWorkers[worker.id] || 0;
            if (level > 0) {
              html += `
                  <div class="worker-slot active-worker">
                      <div class="worker-name">${worker.name}</div>
                      <div class="worker-avatar">
                        <img src="${worker.image}" alt="${worker.name}" style="width: 100%; height: 100%; object-fit: contain;">
                      </div>
                      <div class="worker-level">${level}</div>
                  </div>`;
            } else {
              html += `
                  <div class="worker-slot">
                      <div class="worker-name">???</div>
                      <div class="worker-avatar">
                        <img src="${worker.image}" alt="${worker.name}" style="width: 100%; height: 100%; object-fit: contain;">
                      </div>
                      <div class="worker-level">0</div>
                  </div>`;
            }
          });
          container.innerHTML = html;
        });
    })
    .catch((err) => console.error("Error fetching workers:", err));
}

// 3. رسم صفحة التطوير (Upgrade)
function updateUpgradeUI() {
  const container = document.getElementById("upgrade-container");
  if (!container) return;

  fetch("/api/workers/")
    .then((res) => res.json())
    .then((workers) => {
      fetch("/api/login/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          telegram_id: telegramId,
          referral_id: referralId,
          username: username,
        }),
      })
        .then((res) => res.json())
        .then((userData) => {
          const userWorkers = userData.user.workers;
          let html = "";

          workers.forEach((worker) => {
            const level = userWorkers[worker.id] || 0;
            const currentPrice = Math.floor(
              worker.basePrice * Math.pow(1.5, level),
            );

            html += `
              <div class="col-6 col-md-3">
                  <div class="upgrade-card">
                      <h5>${worker.name}</h5>
                      <img src="${worker.image}" alt="${worker.name}" style="width: 60px; height: 60px; object-fit: contain;">
                      <div class="mb-1">المستوى: <strong>${level}</strong></div>
                      <div class="price"><img src="/static/images/tac-coin.png" alt="" width="18"> ${currentPrice}</div>
                      <button class="btn btn-buy w-100" onclick="buyWorker(${worker.id}, ${currentPrice})">ترقية</button>
                  </div>
              </div>`;
          });
          container.innerHTML = html;
        });
    })
    .catch((err) => console.error("Error fetching workers:", err));
}

// 4. دالة تشغيل الصوت
function playSound(fileName) {
  let audio = new Audio(`/static/sounds/${fileName}`);
  audio.volume = 0.5;
  audio.play().catch((e) => console.log("المتصفح منع تشغيل الصوت", e));
}

// 5. تأثير طيران العملات
function createFlyingCoins() {
  const container = document.getElementById("coin-animation-container");
  const collectBtn = document.querySelector(".btn-collect-board");

  if (!container || !collectBtn) return;

  const rect = collectBtn.getBoundingClientRect();
  const containerRect = container.getBoundingClientRect();

  for (let i = 0; i < 25; i++) {
    let coin = document.createElement("div");
    coin.classList.add("flying-coin");

    let startX = rect.left - containerRect.left + rect.width / 2 - 15;
    let startY = rect.top - containerRect.top + rect.height / 2 - 15;

    coin.style.left = `${startX}px`;
    coin.style.top = `${startY}px`;

    let randomX = (Math.random() - 0.5) * 200;
    let randomY = (Math.random() - 0.5) * 200 - 100;

    coin.style.setProperty("--x", `${randomX}px`);
    coin.style.setProperty("--y", `${randomY}px`);

    container.appendChild(coin);
    setTimeout(() => {
      coin.remove();
    }, 1000);
  }
}

// 6. جمع الجولد
function collectGold() {
  let storage = parseFloat(localStorage.getItem("mineStorage")) || 0;

  if (storage > 0) {
    fetch("/api/collect/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        telegram_id: telegramId,
        amount: storage,
        referral_id: referralId,
      }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.message === "Collected!") {
          localStorage.setItem("mineStorage", 0);
          document.getElementById("current-gold").innerText = "0.0000";

          playSound("coins.wav");
          createFlyingCoins();

          updateBalanceUI(data.new_balance);

          setTimeout(() => {
            Swal.fire({
              title: "تم التجميع!",
              text: `تمت إضافة ${storage.toFixed(2)} TAC إلى رصيدك.`,
              icon: "success",
              timer: 2500,
              showConfirmButton: false,
              customClass: {
                popup: "swal-custom-popup",
                title: "swal-custom-title",
              },
            });
          }, 500);
        }
      })
      .catch((err) => console.error(err));
  } else {
    Swal.fire({
      title: "المخزن فاضي!",
      text: "استنى شوية العمال يجمعوا جولد أو شوف إعلان.",
      icon: "error",
      confirmButtonText: "حسناً",
      customClass: {
        popup: "swal-custom-popup",
        title: "swal-custom-title",
        confirmButton: "swal-confirm-btn",
      },
    });
  }
}

// 7. محاكاة زيادة الجولد (التعدين)
let miningInterval = null;

function applyProduction(production) {
  if (!production) return;
  const perSecond = Number(production.per_hour) / 3600;
  if (!Number.isFinite(perSecond)) return;

  window.productionPerSecond = perSecond;

  const statHourEl = document.getElementById("stat-hour");
  const statDayEl = document.getElementById("stat-day");
  const statMonthEl = document.getElementById("stat-month");
  if (statHourEl) statHourEl.innerText = Number(production.per_hour).toFixed(2);
  if (statDayEl) statDayEl.innerText = Number(production.per_day).toFixed(2);
  if (statMonthEl) statMonthEl.innerText = Number(production.per_month).toFixed(2);
}

window.applyProduction = applyProduction;

function simulateMining() {
  let storageEl = document.getElementById("current-gold");
  if (storageEl) {
    let storage = parseFloat(localStorage.getItem("mineStorage")) || 0;
    let perSecond = window.productionPerSecond || 0;

    if (storage < 500 && perSecond > 0) {
      storage += perSecond;
      localStorage.setItem("mineStorage", storage);
      storageEl.innerText = storage.toFixed(4);
    }
  }
}
// 8. دالة رسم لوحة المتصدرين
let topPlayers = [
  {
    rank: 1,
    name: "CryptoKing",
    score: 1250000,
    class: "top-1",
    icon: "fa-crown text-warning",
  },
  {
    rank: 2,
    name: "Luna_Miner",
    score: 980500,
    class: "top-2",
    icon: "fa-user-astronaut text-light",
  },
  {
    rank: 3,
    name: "ShadowStrike",
    score: 850000,
    class: "top-3",
    icon: "fa-user-ninja text-danger",
  },
  {
    rank: 4,
    name: "NeonViper",
    score: 720000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 5,
    name: "GhostProtocol",
    score: 680000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 6,
    name: "Titanfall",
    score: 610000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 7,
    name: "VortexX",
    score: 550000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 8,
    name: "CyberWolf",
    score: 490000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 9,
    name: "Rogue_AI",
    score: 420000,
    class: "normal",
    icon: "fa-user text-info",
  },
  {
    rank: 10,
    name: "PixelGhost",
    score: 350000,
    class: "normal",
    icon: "fa-user text-info",
  },
];

function renderLeaderboard() {
  const container = document.getElementById("leaderboard-list");
  if (!container) return;

  let html = "";

  topPlayers.forEach((player) => {
    let itemClass =
      player.class === "normal" ? "leader-item" : `leader-item ${player.class}`;

    let badgeColor = "";
    if (player.rank === 1) badgeColor = "bg-warning text-dark";
    else if (player.rank === 2) badgeColor = "bg-light text-dark";
    else if (player.rank === 3) badgeColor = "bg-danger text-white";
    else badgeColor = "bg-secondary text-white";

    html += `
      <div class="${itemClass}" style="padding: 6px 10px; margin-bottom: 5px;">
        <div class="rank-badge ${badgeColor}" style="width: 22px; height: 22px; font-size: 11px; margin-right: 8px;">${player.rank}</div>
        <div class="leader-avatar" style="width: 28px; height: 28px; margin-right: 8px; font-size: 12px;">
            <i class="fa-solid ${player.icon}"></i>
        </div>
        <div class="leader-info">
          <span class="leader-name" style="font-size: 12px;">${player.name}</span>
        </div>
        <div class="leader-score" style="font-size: 11px;">
            <i class="fa-solid fa-coins text-warning"></i> ${player.score.toLocaleString("en-US")}
        </div>
      </div>
    `;
  });

  // كارت اليوزر الحالي
  html += `
    <div class="leader-item my-rank" style="margin-top: 10px; border: 2px solid var(--cyan); padding: 8px 10px;">
      <div class="leader-avatar" style="width: 28px; height: 28px; margin-right: 8px; font-size: 12px;">
      </div>
      <div class="leader-score" style="font-size: 11px;">
      ----------------------------------------
      </div>
    </div>
  `;

  container.innerHTML = html;
}

// دالة تحديث الأرباح عشوائياً كل 30 ثانية
function updateLeaderboardScores() {
  topPlayers.forEach((player) => {
    let randomIncrease = Math.floor(Math.random() * 200) + 100;
    player.score += randomIncrease;
  });

  topPlayers.sort((a, b) => b.score - a.score);

  topPlayers.forEach((player, index) => {
    player.rank = index + 1;
    if (player.rank === 1) {
      player.class = "top-1";
      player.icon = "fa-crown text-warning";
    } else if (player.rank === 2) {
      player.class = "top-2";
      player.icon = "fa-user-astronaut text-light";
    } else if (player.rank === 3) {
      player.class = "top-3";
      player.icon = "fa-user-ninja text-danger";
    } else {
      player.class = "normal";
      player.icon = "fa-user text-info";
    }
  });

  renderLeaderboard();
}

setInterval(updateLeaderboardScores, 30000);

// 9. التشغيل عند تحميل الصفحة
document.addEventListener("DOMContentLoaded", () => {
  // 1. تسجيل الدخول وجلب بيانات اليوزر
  fetch("/api/login/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      telegram_id: telegramId,
      username: username,
      referral_id: referralId,
    }),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.message === "Success") {
        updateBalanceUI(data.user.balance);

        const displayName = data.user.username || username;
        document.querySelectorAll(".my-user, #profile-username").forEach((el) => {
          if (displayName && displayName !== "new user") el.textContent = displayName;
        });

        let totalLevels = 0;
        for (let workerId in data.user.workers) {
          totalLevels += data.user.workers[workerId];
        }

        const capacityBar = document.querySelector(".progress-bar-fill");
        const capacityText = document.querySelector(".capacity-text");
        const maxCapacity = 800;

        if (capacityBar && capacityText) {
          let percentage = (totalLevels / maxCapacity) * 100;
          if (percentage > 100) percentage = 100;
          capacityBar.style.width = `${percentage}%`;
          capacityText.innerText = `${totalLevels} / ${maxCapacity}`;
        }

        renderHomeGrid();
        updateUpgradeUI();
        renderLeaderboard();

        applyProduction(data.user.production);

        if (document.getElementById("mine-grid")) {
          if (miningInterval) clearInterval(miningInterval);
          miningInterval = setInterval(simulateMining, 1000);
        }

        let storage = parseFloat(localStorage.getItem("mineStorage")) || 0;
        const goldEl = document.getElementById("current-gold");
        if (goldEl) goldEl.innerText = storage.toFixed(4);
      }
    })
    .catch((err) => console.error("API Error:", err));

  // 2. تفعيل الأيقونة النشطة في الشريط السفلي
  let path = window.location.pathname;
  let currentPath = path === "/" || path === "" ? "/" : path;

  const navItems = document.querySelectorAll(".nav-item");
  navItems.forEach((item) => {
    let href = item.getAttribute("href");
    if (href === currentPath) {
      item.classList.add("active");
    } else if (
      (href === "/" && path.includes("index")) ||
      (href === "/index/" && path === "/")
    ) {
      item.classList.add("active");
    } else {
      item.classList.remove("active");
    }
  });

  // 3. صوت الكليك لأي زرار
  const allButtons = document.querySelectorAll(
    "button, .nav-item, .action-btn",
  );
  allButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      playSound("click.wav");
    });
  });
});
