"use strict";

const $ = (id) => document.getElementById(id);
const createForm = $("create-form");
const fetchForm = $("fetch-form");
let campaign = null;
let optionsReady = false;
let loadingOptions = false;
let fetching = false;
let pendingStreams = 0;
let previousCreation = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, className, onClick) {
  const node = element("button", text, className);
  node.type = onClick ? "button" : "submit";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

function status(node, message, error = false) {
  node.textContent = message;
  node.dataset.error = String(error);
}

async function api(path, method = "GET", body, extraHeaders = {}) {
  let response;
  try {
    response = await fetch("/api" + path, {
      method,
      headers: { ...(body && { "Content-Type": "application/json" }), ...extraHeaders },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("Нет связи с приложением. Проверьте соединение и повторите запрос.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const validation = Array.isArray(detail) && detail.map((item) => item.msg).join(", ");
    const error = new Error((typeof detail === "string" && detail) || validation || "Ошибка запроса (HTTP " + response.status + ").");
    error.status = response.status;
    throw error;
  }
  return data;
}

function positiveId(value) {
  const text = value.trim();
  const number = Number(text);
  return /^[1-9]\d*$/.test(text) && Number.isSafeInteger(number) ? number : null;
}

function offerLookup(title) {
  const root = element("div", null, "lookup");
  const label = element("label", title);
  const input = element("input");
  input.type = "search";
  input.required = true;
  input.maxLength = 255;
  input.autocomplete = "off";
  input.placeholder = "ID или название оффера";
  const list = element("ul");
  list.hidden = true;
  const hint = element("span", "", "hint");
  hint.setAttribute("role", "status");
  label.append(input);
  root.append(label, list, hint);
  let timer;
  let version = 0;

  function choose(offer) {
    input.value = String(offer.id);
    input.setCustomValidity("");
    list.hidden = true;
    hint.textContent = offer.name;
    input.focus();
  }

  input.addEventListener("input", () => {
    const requestVersion = ++version;
    clearTimeout(timer);
    input.setCustomValidity("");
    list.hidden = true;
    hint.textContent = "";
    const query = input.value.trim();
    if (!query) return;
    timer = setTimeout(async () => {
      try {
        const directId = positiveId(query);
        if (directId) {
          const offer = await api("/offers/" + directId);
          if (version === requestVersion) hint.textContent = offer.name;
          return;
        }
        const offers = await api("/offers?q=" + encodeURIComponent(query));
        if (version !== requestVersion) return;
        list.replaceChildren();
        for (const offer of offers.slice(0, 20)) {
          const item = element("li");
          item.append(button("#" + offer.id + " · " + offer.name, "", () => choose(offer)));
          list.append(item);
        }
        list.hidden = !list.childElementCount;
        if (list.hidden) hint.textContent = "Совпадений нет.";
      } catch (error) {
        if (version === requestVersion) {
          hint.textContent = error.message;
          if (error.status === 404) input.setCustomValidity(error.message);
        }
      }
    }, 240);
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      version++;
      list.hidden = true;
      input.focus();
    }
    if (event.key === "ArrowDown" && event.target === input && !list.hidden) {
      event.preventDefault();
      list.querySelector("button")?.focus();
    }
  });
  return {
    root,
    id() {
      const id = positiveId(input.value);
      if (!id) {
        input.setCustomValidity("Выберите оффер из результатов поиска или введите его числовой ID.");
        input.reportValidity();
      } else {
        version++;
        list.hidden = true;
      }
      return id;
    },
  };
}

const creationOffer = offerLookup("Оффер для других стран");
$("create-offer").append(creationOffer.root);

async function loadOptions() {
  if (loadingOptions || optionsReady) return;
  loadingOptions = true;
  $("create-button").disabled = true;
  const feedback = $("options-feedback");
  status(feedback, "Загружаем настройки…");
  try {
    const data = await api("/options");
    const choices = { domain_id: data.domains, group_id: data.groups, traffic_source_id: data.traffic_sources };
    for (const [field, entries] of Object.entries(choices)) {
      const select = createForm.elements[field];
      select.replaceChildren(new Option("Выбрать", ""));
      for (const entry of entries) select.add(new Option(entry.name, String(entry.id)));
    }
    optionsReady = Object.values(choices).every((items) => items.length > 0);
    status(feedback, optionsReady ? "" : "В трекере нужен хотя бы один домен, группа кампаний и источник.");
  } catch (error) {
    status(feedback, error.message, true);
  } finally {
    loadingOptions = false;
    $("create-button").disabled = !optionsReady;
    if (!optionsReady) feedback.append(button("Повторить", "", loadOptions));
  }
}

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("create-button").disabled) return;
  const offerId = creationOffer.id();
  if (!offerId) return;
  const fields = createForm.elements;
  const body = {
    name: fields.name.value.trim(), country: fields.country.value.toUpperCase(),
    offer_id: offerId, domain_id: Number(fields.domain_id.value),
    group_id: Number(fields.group_id.value), traffic_source_id: Number(fields.traffic_source_id.value),
  };
  if (!body.name) {
    fields.name.value = "";
    fields.name.reportValidity();
    return;
  }
  const fingerprint = JSON.stringify(body);
  if (previousCreation?.fingerprint !== fingerprint) previousCreation = { fingerprint, key: crypto.randomUUID() };
  const feedback = $("create-feedback");
  $("create-button").disabled = true;
  for (const fs of createForm.querySelectorAll("fieldset")) fs.disabled = true;
  status(feedback, "Создаём кампанию…");
  try {
    const result = await api("/campaigns", "POST", body, { "Idempotency-Key": previousCreation.key });
    status(feedback, "Кампания #" + result.id + " создана" + (result.alias ? " · " + result.alias : "") + ".");
    feedback.append(button("Открыть в редакторе", "", () => {
      fetchForm.elements.campaign_id.value = result.id;
      location.hash = "editor";
      loadCampaign(result.id, true);
    }));
  } catch (error) {
    status(feedback, error.message, true);
  } finally {
    for (const fs of createForm.querySelectorAll("fieldset")) fs.disabled = false;
    $("create-button").disabled = false;
  }
});

function updateFetchAvailability() {
  for (const input of fetchForm.elements) input.disabled = fetching || pendingStreams > 0;
}

async function loadCampaign(id, refresh) {
  if (fetching || pendingStreams) return;
  fetching = true;
  updateFetchAvailability();
  const streamList = $("streams");
  streamList.inert = true;
  const feedback = $("editor-feedback");
  status(feedback, refresh ? "Загружаем кампанию из трекера…" : "Открываем сохранённую кампанию…");
  try {
    campaign = await api("/campaigns/" + id + (refresh ? "/fetch" : ""), refresh ? "POST" : "GET");
    renderCampaign();
    const url = new URL(location.href);
    url.searchParams.set("campaign", String(id));
    history.replaceState(null, "", url);
    status(feedback, "");
  } catch (error) {
    status(feedback, error.message, true);
  } finally {
    fetching = false;
    streamList.inert = false;
    updateFetchAvailability();
  }
}

fetchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const id = positiveId(fetchForm.elements.campaign_id.value);
  if (id) loadCampaign(id, true);
});

function renderCampaign() {
  $("workspace").hidden = false;
  $("streams-heading").textContent = "Потоки кампании #" + campaign.campaign_id;
  const list = $("streams");
  list.replaceChildren(...campaign.streams.map(streamCard));
  if (!campaign.streams.length) list.append(element("p", "В кампании нет потоков.", "info"));
}

function streamCard(stream) {
  const card = element("article", null, "panel stream");
  card.dataset.dirty = String(stream.dirty);
  const header = element("header", null, "stream-heading");
  const title = element("h3", stream.name || "Поток");
  title.tabIndex = -1;
  header.append(title, element("span", "#" + stream.id, "stream-id"));
  if (stream.dirty) header.append(element("span", "Черновик", "status-tag"));
  const body = element("fieldset", null, "stream-body");
  body.setAttribute("aria-label", "Офферы потока " + stream.id);
  const feedback = element("p", "", "feedback");
  feedback.setAttribute("role", "status");
  card.append(header, body);

  function replaceStream(updated, message, error = false) {
    const index = campaign.streams.findIndex((item) => item.id === stream.id);
    if (!updated || (updated.missing && !updated.dirty)) {
      campaign.streams.splice(index, 1);
      card.remove();
      if (!campaign.streams.length) renderCampaign();
      status($("editor-feedback"), message, error);
      return;
    }
    campaign.streams[index] = updated;
    const next = streamCard(updated);
    card.replaceWith(next);
    status(next.querySelector(".feedback"), message, error);
    next.querySelector("h3").focus({ preventScroll: true });
  }

  async function mutate(path, method, payload, message) {
    if (body.disabled || fetching) return;
    body.disabled = true;
    pendingStreams++;
    updateFetchAvailability();
    status(feedback, "Выполняем запрос…");
    try {
      const updated = await api("/streams/" + stream.id + path, method, payload);
      replaceStream(updated, message);
    } catch (error) {
      status(feedback, error.message, true);
      if (path === "/push" && error.status === 409) {
        const latest = await api("/campaigns/" + campaign.campaign_id).catch(() => null);
        if (latest) replaceStream(latest.streams.find((item) => item.id === stream.id), error.message, true);
      }
    } finally {
      body.disabled = false;
      pendingStreams--;
      updateFetchAvailability();
    }
  }

  if (stream.conflict || stream.missing) {
    const text = stream.missing ? "Поток удалён в трекере." : "В трекере состав потока изменился.";
    body.append(element("p", text + " Отмените черновик, чтобы принять загруженное состояние.", "info"));
  }
  if (stream.offers.length) {
    const list = element("div", null, "offer-list");
    for (const offer of stream.offers) list.append(offerRow(offer, mutate, stream.missing));
    body.append(list);
  }
  if ((stream.editable ?? stream.offers.length > 0) && !stream.missing) {
    const add = element("form", null, "offer-form");
    const lookup = offerLookup("Добавить оффер");
    add.append(lookup.root, button("Добавить"));
    add.addEventListener("submit", (event) => {
      event.preventDefault();
      const offerId = lookup.id();
      if (offerId) mutate("/offers", "POST", { offer_id: offerId }, "Оффер добавлен в черновик.");
    });
    body.append(add);
  } else if (!stream.offers.length) {
    body.append(element("p", "В этом потоке нет офферов.", "info"));
  }
  if (stream.dirty || stream.conflict || stream.missing) {
    const actions = element("div", "", "stream-actions");
    const push = button("Отправить в трекер", "primary", () => mutate("/push", "POST", undefined, "Изменения отправлены в трекер."));
    push.disabled = Boolean(stream.conflict || stream.missing || !stream.dirty);
    actions.append(push, button("Отменить изменения", "", () => mutate("/cancel", "POST", undefined, "Черновые изменения отменены.")));
    body.append(actions);
  }
  body.append(feedback);
  return card;
}

function offerRow(offer, mutate, missing) {
  const pending = offer.changed || offer.pending_removal || offer.pending_addition;
  const row = element("div", null, "offer-row" + (pending ? " pending" : offer.archived ? " archived" : ""));
  const title = element("div", null, "offer-title");
  title.append(element("strong", offer.name), element("small", "Оффер #" + offer.offer_id));
  const state = offer.pending_removal ? "Будет удалён при отправке" : offer.pending_addition ? "Будет добавлен при отправке" : offer.archived ? "В архиве" : offer.changed ? "Изменён в черновике" : "";
  if (state) title.append(element("small", state, pending ? "change" : ""));
  const patch = (change) => mutate("/offers/" + offer.offer_id, "PATCH", change, "Черновик сохранён.");
  row.append(title);
  if (!missing && offer.pinned && !offer.archived) {
    const form = element("form", null, "share share-form");
    const amount = element("input");
    Object.assign(amount, { type: "number", min: "0", max: "100", step: "1", required: true, value: String(offer.share) });
    amount.setAttribute("aria-label", "Доля оффера " + offer.name + ", в процентах");
    const save = button("Применить");
    form.append(amount, element("span", "%"), save);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      patch({ pinned: true, share: Number(amount.value) });
    });
    row.append(form);
  } else {
    row.append(element("span", offer.share + " %", "share"));
  }
  if (!offer.archived && !missing) {
    const pin = element("label", null, "pin");
    const check = element("input");
    check.type = "checkbox";
    check.checked = offer.pinned;
    check.setAttribute("aria-label", "Закрепить долю оффера " + offer.name);
    check.addEventListener("change", () => {
      patch({ pinned: check.checked }).finally(() => {
        if (check.isConnected) check.checked = offer.pinned;
      });
    });
    pin.append(check, document.createTextNode("Закрепить"));
    row.append(pin);
  } else row.append(element("span"));
  if (!missing) {
    const action = offer.pending_removal ? "Оставить" : offer.archived ? "Восстановить" : "Удалить";
    row.append(button(action, "row-action", () => patch({ archived: !offer.archived })));
  }
  return row;
}

function showPage() {
  const edit = location.hash === "#editor";
  $("create").hidden = edit;
  $("editor").hidden = !edit;
  $("nav-create").toggleAttribute("aria-current", !edit);
  $("nav-editor").toggleAttribute("aria-current", edit);
  $(edit ? "nav-editor" : "nav-create").setAttribute("aria-current", "page");
  document.title = (edit ? "Редактор кампании" : "Создание кампании") + " · Adrobot";
  if (!edit) loadOptions();
}

window.addEventListener("hashchange", showPage);
const rememberedId = positiveId(new URL(location.href).searchParams.get("campaign") || "");
if (rememberedId) {
  fetchForm.elements.campaign_id.value = rememberedId;
  if (!location.hash) history.replaceState(null, "", location.pathname + location.search + "#editor");
  loadCampaign(rememberedId, false);
}
showPage();
