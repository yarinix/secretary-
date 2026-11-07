/* Секретарь · Telegram Mini App
   Данные — только через POST /api/state и /api/settings с подписанным initData.
   initDataUnsafe не используется вообще. */
(() => {
  "use strict";

  const TG = window.Telegram && window.Telegram.WebApp;
  const INIT = (TG && TG.initData) || "";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const BOOL_KEYS = ["enabled", "inbox", "notify", "smart_replies", "night_enabled"];
  const INT_KEYS = [
    "reply_delay_minutes", "away_minutes", "away_collect_seconds",
    "max_replies_per_hour", "history_limit", "persona_limit"
  ];
  const CLOCK_KEYS = ["night_start", "night_end"];
  const CLOCK_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;
  const FIELD_IDS = {
    reply_delay_minutes: "#replyDelay", away_minutes: "#awayMinutes",
    away_collect_seconds: "#awayCollect", night_start: "#nightStart",
    night_end: "#nightEnd", max_replies_per_hour: "#maxReplies",
    history_limit: "#history", persona_limit: "#personaLimit",
    owner_name: "#ownerName", timezone: "#tz", persona: "#persona",
    night_hint: "#nightHint"
  };
  const READONLY_LABELS = {
    BOT_TOKEN: "BOT_TOKEN", OWNER_ID: "OWNER_ID", WEBHOOK_SECRET: "WEBHOOK_SECRET",
    PORT: "PORT", DB_PATH: "DB_PATH", BASE_URL: "BASE_URL", APP_URL: "Mini App URL",
    STORAGE_BACKEND: "Хранилище настроек",
    api_key_set: "Ключ Gemini (ANTHROPIC_API_KEY)"
  };

  let state = {};
  let schema = { fields: {}, timezones: [], readonly: [] };
  let readonly = {};
  let baseline = "";
  let locked = false;
  let saving = false;

  const nativeButton = !!(TG && TG.MainButton && typeof TG.MainButton.show === "function");

  /* ---------- Тема ---------- */
  function applyTheme() {
    const scheme = TG
      ? TG.colorScheme
      : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = scheme === "dark" ? "dark" : "light";
  }

  /* ---------- Haptic / Toast ---------- */
  function haptic(kind) {
    const h = TG && TG.HapticFeedback;
    if (!h) return;
    try {
      if (kind === "success") h.notificationOccurred("success");
      else if (kind === "error") h.notificationOccurred("error");
      else h.impactOccurred("light");
    } catch (_) { /* старый клиент */ }
  }
  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = "toast"; }, 3000);
  }

  /* ---------- API ---------- */
  async function api(path, extra = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ initData: INIT }, extra))
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* не-JSON ответ */ }
    if (!response.ok) {
      const err = new Error(data.error || ("HTTP " + response.status));
      err.status = response.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function lock(title, text) {
    locked = true;
    document.body.classList.add("locked");
    $("#gate").hidden = false;
    $("#gateTitle").textContent = title;
    $("#gateText").textContent = text;
    $$("input, select, textarea, button").forEach(el => { el.disabled = true; });
    showSave(false);
  }

  /* ---------- Рендер ---------- */
  function applySchema() {
    for (const key of INT_KEYS) {
      const f = schema.fields && schema.fields[key];
      const el = $(FIELD_IDS[key]);
      // Границы берём только у полей с типом int: у clock их нет.
      if (f && f.type === "int" && el) {
        if (f.min !== undefined) el.min = f.min;
        if (f.max !== undefined) el.max = f.max;
      }
    }
    const tz = $("#tz");
    const list = (schema.timezones && schema.timezones.length)
      ? schema.timezones : [state.timezone || "Asia/Omsk"];
    tz.innerHTML = list.map(z => `<option value="${esc(z)}">${esc(z)}</option>`).join("");
    updatePersonaLimit();
    updateNightHintCounter();
  }

  function updatePersonaLimit() {
    const limit = Number(collect().persona_limit) || Number(state.persona_limit) || 800;
    $("#persona").maxLength = limit;
    $("#personaCounter").textContent = `${$("#persona").value.length} / ${limit}`;
  }

  function updateNightHintCounter() {
    const f = (schema.fields || {}).night_hint || {};
    const limit = Number(f.max) || 300;
    const el = $("#nightHint");
    el.maxLength = limit;
    $("#nightHintCounter").textContent = `${el.value.length} / ${limit}`;
  }

  function render() {
    BOOL_KEYS.forEach(k => {
      const el = $(`input[data-k="${k}"]`);
      if (el) el.checked = state[k] === "1";
    });
    INT_KEYS.forEach(k => {
      const el = $(FIELD_IDS[k]);
      if (el) el.value = state[k];
    });
    CLOCK_KEYS.forEach(k => {
      const el = $(FIELD_IDS[k]);
      if (el) el.value = state[k] || "";
    });
    $("#persona").value = state.persona || "";
    $("#nightHint").value = state.night_hint || "";
    $("#ownerName").value = state.owner_name || "";
    $("#tz").value = state.timezone || "Asia/Omsk";
    updatePersonaLimit();
    updateNightHintCounter();
    renderIgnore();
    renderModels();
    renderReadonly();
  }

  function renderIgnore() {
    const box = $("#ignoreChips");
    const ids = state.ignore_ids || [];
    box.innerHTML = ids.length
      ? ids.map(id => `<span class="chip">${esc(id)}<button class="chip__x" data-id="${esc(id)}" aria-label="Удалить">×</button></span>`).join("")
      : '<div class="chips__empty">Список пуст</div>';
    $$(".chip__x", box).forEach(b => b.addEventListener("click", () => {
      state.ignore_ids = ids.filter(x => String(x) !== b.dataset.id);
      haptic(); renderIgnore(); updateDirty();
    }));
  }

  function renderModels() {
    const box = $("#modelList");
    const models = state.models || [];
    box.innerHTML = models.map((m, i) => `
      <div class="model">
        <span class="model__num">${i + 1}</span>
        <div>
          <div class="model__name">${esc(m)}</div>
          <div class="model__tag">${i === 0 ? "основная" : "резерв №" + i}</div>
        </div>
        <div class="model__acts">
          <button class="act up" data-i="${i}" ${i === 0 ? "disabled" : ""} title="Выше">↑</button>
          <button class="act down" data-i="${i}" ${i === models.length - 1 ? "disabled" : ""} title="Ниже">↓</button>
          <button class="act del" data-i="${i}" title="Удалить">✕</button>
        </div>
      </div>`).join("");
    $$(".act", box).forEach(b => b.addEventListener("click", () => {
      const i = Number(b.dataset.i);
      if (b.classList.contains("up") && i > 0)
        [models[i - 1], models[i]] = [models[i], models[i - 1]];
      if (b.classList.contains("down") && i < models.length - 1)
        [models[i + 1], models[i]] = [models[i], models[i + 1]];
      if (b.classList.contains("del")) {
        if (models.length === 1) return toast("Должна остаться хотя бы одна модель", true);
        models.splice(i, 1);
      }
      haptic(); renderModels(); updateDirty();
    }));
    $("#cascadeHint").textContent = models.length
      ? `${models.length} ${models.length === 1 ? "модель" : "модели"}: ${models.join(" → ")}`
      : "список пуст";
  }

  function renderReadonly() {
    const card = $("#readonlyCard");
    const keys = (schema.readonly || Object.keys(READONLY_LABELS)).filter(k => k in readonly);
    if (!keys.length) {
      card.innerHTML = '<div class="kv"><span class="kv__k">Нет данных</span><span class="kv__v">—</span></div>';
      return;
    }
    card.innerHTML = keys.map(k => {
      let v = readonly[k];
      if (k === "api_key_set") v = v ? "задан ✓" : "НЕ задан";
      const cls = (k === "api_key_set" && readonly[k]) ? ' style="color:var(--tg-green)"' : "";
      return `<div class="kv"><span class="kv__k">${esc(READONLY_LABELS[k] || k)}</span><span class="kv__v"${cls}>${esc(v)}</span></div>`;
    }).join("");

    const badge = $("#keyBadge");
    const keySet = readonly.api_key_set;
    badge.className = "badge " + (keySet ? "badge--ok" : "badge--bad");
    badge.textContent = keySet ? "ключ задан" : "нет ключа";
  }

  function renderStatus(status) {
    const s = status || {};
    const connected = !!s.connected;
    const badge = $("#connBadge");
    badge.className = "badge " + (connected ? "badge--ok" : "badge--bad");
    badge.innerHTML = `<span class="badge__dot"></span>${connected ? "Подключён" : "Нет подключения"}`;
    $("#connHint").textContent = connected
      ? (s.can_reply ? "Telegram Business, можно отвечать" : "Подключён, но нет права отвечать")
      : "Подключите бота в Telegram для бизнеса";
    const bits = [];
    if (s.pending_chats !== undefined) bits.push(`в очереди: ${s.pending_chats}`);
    if (s.owner_away_minutes !== undefined && s.owner_away_minutes !== null)
      bits.push(`вас нет: ${s.owner_away_minutes} мин`);
    if (s.pending_chats && s.next_reply_in !== undefined && s.next_reply_in !== null)
      bits.push(`ответ через ~${s.next_reply_in} сек`);
    if (s.replies_last_hour !== undefined) bits.push(`ответов за час: ${s.replies_last_hour}`);
    if (s.night_now) bits.push("🌙 сейчас ночь");
    $("#appSub").textContent = bits.length ? bits.join(" · ") : "Управление ботом";
  }

  /* ---------- Форма ---------- */
  function collect() {
    const out = {};
    BOOL_KEYS.forEach(k => out[k] = $(`input[data-k="${k}"]`).checked ? "1" : "0");
    INT_KEYS.forEach(k => out[k] = $(FIELD_IDS[k]).value.trim());
    CLOCK_KEYS.forEach(k => out[k] = $(FIELD_IDS[k]).value.trim());
    out.persona = $("#persona").value;
    out.night_hint = $("#nightHint").value;
    out.owner_name = $("#ownerName").value.trim();
    out.timezone = $("#tz").value;
    out.ignore_ids = Array.from(state.ignore_ids || []);
    out.models = Array.from(state.models || []);
    return out;
  }

  function clientErrors(v) {
    const errors = {};
    for (const k of INT_KEYS) {
      const f = (schema.fields || {})[k] || {};
      const lo = f.min !== undefined ? f.min : 0;
      const hi = f.max !== undefined ? f.max : 1e9;
      const n = Number(v[k]);
      if (!/^\d+$/.test(v[k]) || n < lo || n > hi) errors[k] = `допустимо ${lo}–${hi}`;
    }
    for (const k of CLOCK_KEYS) {
      if (!CLOCK_RE.test(v[k])) errors[k] = "формат ЧЧ:ММ";
    }
    const limit = Number(v.persona_limit) || 800;
    if (v.persona.length > limit) errors.persona = `не длиннее ${limit} символов`;
    const hintMax = (schema.fields && schema.fields.night_hint && schema.fields.night_hint.max) || 300;
    if (v.night_hint.length > hintMax) errors.night_hint = `не длиннее ${hintMax} символов`;
    const nameMax = (schema.fields && schema.fields.owner_name && schema.fields.owner_name.max) || 40;
    if (!v.owner_name || v.owner_name.length > nameMax) errors.owner_name = `1–${nameMax} символов`;
    if (!v.models.length) errors.models = "нужна хотя бы одна модель";
    return errors;
  }

  function markErrors(errors) {
    $$("input, select, textarea").forEach(el => el.classList.remove("invalid"));
    Object.keys(errors || {}).forEach(k => {
      const sel = FIELD_IDS[k];
      if (sel) $(sel) && $(sel).classList.add("invalid");
    });
  }

  function snapshot(v) { return JSON.stringify(v); }

  function updateDirty() {
    if (locked) return;
    const dirty = snapshot(collect()) !== baseline;
    showSave(dirty);
    if (TG) {
      try { dirty ? TG.enableClosingConfirmation() : TG.disableClosingConfirmation(); } catch (_) {}
    }
  }

  function showSave(show) {
    if (nativeButton) {
      try { show ? TG.MainButton.show() : TG.MainButton.hide(); } catch (_) {}
    } else {
      $("#mbWrap").classList.toggle("visible", show);
    }
  }

  function setBusy(busy) {
    if (nativeButton) {
      try { busy ? TG.MainButton.showProgress() : TG.MainButton.hideProgress(); } catch (_) {}
    } else {
      $("#saveBtn").disabled = busy;
      $("#saveBtn").textContent = busy ? "Сохраняю…" : "Сохранить";
    }
  }

  async function save() {
    if (locked || saving) return;
    const patch = collect();
    const errors = clientErrors(patch);
    markErrors(errors);
    if (Object.keys(errors).length) {
      haptic("error");
      return toast("Проверьте поля: " + Object.values(errors)[0], true);
    }
    saving = true;
    setBusy(true);
    try {
      const data = await api("/api/settings", { settings: patch });
      state = data.settings || state;
      if (data.schema) schema = data.schema;
      render();
      renderStatus(data.status);
      baseline = snapshot(collect());
      showSave(false);
      if (TG) { try { TG.disableClosingConfirmation(); } catch (_) {} }
      haptic("success");
      toast("Сохранено — применится к следующему сообщению");
    } catch (e) {
      if (e.data && e.data.errors) markErrors(e.data.errors);
      if (e.status === 401 || e.status === 403) {
        lock("Сессия устарела", "Откройте приложение заново из бота.");
      } else {
        toast((e.data && e.data.error) || "Не удалось сохранить", true);
      }
      haptic("error");
    } finally {
      saving = false;
      setBusy(false);
    }
  }

  /* ---------- События ---------- */
  $$("input[data-k], select[data-k], textarea[data-k]").forEach(el => {
    el.addEventListener("input", () => {
      if (el.id === "persona" || el.id === "personaLimit") updatePersonaLimit();
      if (el.id === "nightHint") updateNightHintCounter();
      el.classList.remove("invalid");
      updateDirty();
    });
    el.addEventListener("change", () => { if (el.type === "checkbox") haptic(); updateDirty(); });
  });

  $$(".preset").forEach(b => b.addEventListener("click", () => {
    $("#persona").value = b.dataset.preset;
    updatePersonaLimit(); haptic(); updateDirty();
  }));
  $("#personaClear").addEventListener("click", () => {
    $("#persona").value = ""; updatePersonaLimit(); haptic(); updateDirty();
  });

  $("#ignoreAdd").addEventListener("click", () => {
    const inp = $("#ignoreInput");
    const id = Number(inp.value.trim());
    if (!Number.isInteger(id) || id <= 0) return toast("Введите положительный Telegram ID", true);
    if ((state.ignore_ids || []).includes(id)) return toast("Такой ID уже есть", true);
    if ((state.ignore_ids || []).length >= 100) return toast("Максимум 100 ID", true);
    state.ignore_ids = (state.ignore_ids || []).concat(id);
    inp.value = ""; haptic(); renderIgnore(); updateDirty();
  });
  $("#ignoreInput").addEventListener("keydown", e => { if (e.key === "Enter") $("#ignoreAdd").click(); });

  $("#modelAdd").addEventListener("click", () => {
    const inp = $("#modelInput");
    const m = inp.value.trim();
    if (!/^[A-Za-z0-9._:-]{1,80}$/.test(m)) return toast("Недопустимое имя модели", true);
    if ((state.models || []).includes(m)) return toast("Модель уже в каскаде", true);
    if ((state.models || []).length >= 8) return toast("Максимум 8 моделей", true);
    state.models = (state.models || []).concat(m);
    inp.value = ""; haptic(); renderModels(); updateDirty();
  });
  $("#modelInput").addEventListener("keydown", e => { if (e.key === "Enter") $("#modelAdd").click(); });

  $("#saveBtn").addEventListener("click", save);

  $("#testAi").addEventListener("click", async () => {
    const btn = $("#testAi"), res = $("#aiResult"), txt = $("#aiResultText");
    btn.disabled = true; btn.textContent = "…";
    try {
      const d = await api("/api/test-ai");
      txt.textContent = d.reply || "Пустой ответ модели";
      res.classList.add("show");
      haptic("success");
    } catch (e) {
      txt.textContent = (e.data && e.data.error) || "Запрос не удался";
      res.classList.add("show");
      toast("Каскад не ответил", true);
      haptic("error");
    } finally {
      btn.disabled = false; btn.textContent = "Запустить";
    }
  });

  /* ---------- Старт ---------- */
  applyTheme();
  if (TG) {
    try {
      TG.ready();
      TG.expand();
      TG.onEvent("themeChanged", applyTheme);
      if (nativeButton) {
        document.body.classList.add("tg-mainbutton");
        TG.MainButton.setText("Сохранить");
        TG.MainButton.hide();
        TG.MainButton.onClick(save);
      }
    } catch (_) { /* старый клиент — работаем с DOM-кнопкой */ }
  }

  (async () => {
    try {
      const data = await api("/api/state");
      schema = data.schema || schema;
      state = data.settings || {};
      readonly = data.readonly || {};
      applySchema();
      render();
      renderStatus(data.status);
      baseline = snapshot(collect());
      showSave(false);
    } catch (e) {
      if (e.status === 401 || e.status === 403 || !INIT) {
        lock("Откройте приложение из бота",
          "Настройки доступны только владельцу и только внутри Telegram. Нажмите кнопку «Настройки» в меню бота.");
      } else {
        lock("Не удалось загрузить настройки", (e.data && e.data.error) || "Попробуйте открыть приложение позже.");
      }
    }
  })();
})();
