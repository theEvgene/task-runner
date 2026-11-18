import { JiraProtocolError } from "./protocol.mjs";

const ENTITIES = new Map([
  ["amp", "&"],
  ["quot", '"'],
  ["apos", "'"],
  ["lt", "<"],
  ["gt", ">"],
]);

export function decodeHtml(value) {
  return String(value).replace(
    /&(#x[0-9a-f]+|#\d+|[a-z]+);/gi,
    (match, entity) => {
      if (entity.startsWith("#x"))
        return String.fromCodePoint(Number.parseInt(entity.slice(2), 16));
      if (entity.startsWith("#"))
        return String.fromCodePoint(Number.parseInt(entity.slice(1), 10));
      return ENTITIES.get(entity.toLowerCase()) ?? match;
    }
  );
}

function attributes(tag) {
  const result = {};
  for (const match of tag.matchAll(
    /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g
  )) {
    result[match[1].toLowerCase()] = decodeHtml(
      match[2] ?? match[3] ?? match[4] ?? ""
    );
  }
  return result;
}

export function defaultsFromEditHtml(html) {
  const values = new Map();
  const add = (name, value) => {
    if (!name) return;
    if (!values.has(name)) values.set(name, []);
    values.get(name).push(value);
  };

  for (const match of String(html ?? "").matchAll(/<input\b[^>]*>/gi)) {
    const attrs = attributes(match[0]);
    const type = (attrs.type || "text").toLowerCase();
    if (!attrs.name || attrs.disabled !== undefined) continue;
    if (["submit", "button", "reset", "file"].includes(type)) continue;
    if (["checkbox", "radio"].includes(type) && attrs.checked === undefined)
      continue;
    add(attrs.name, attrs.value ?? (type === "checkbox" ? "on" : ""));
  }
  for (const match of String(html ?? "").matchAll(
    /<textarea\b([^>]*)>([\s\S]*?)<\/textarea>/gi
  )) {
    const attrs = attributes(match[1]);
    if (attrs.name && attrs.disabled === undefined)
      add(attrs.name, decodeHtml(match[2].replace(/<[^>]*>/g, "")));
  }
  for (const match of String(html ?? "").matchAll(
    /<select\b([^>]*)>([\s\S]*?)<\/select>/gi
  )) {
    const attrs = attributes(match[1]);
    if (!attrs.name || attrs.disabled !== undefined) continue;
    const options = [...match[2].matchAll(/<option\b[^>]*>/gi)].map((item) =>
      attributes(item[0])
    );
    const selected = options.filter((item) => item.selected !== undefined);
    const chosen = selected.length ? selected : options.slice(0, 1);
    for (const option of chosen) add(attrs.name, option.value ?? "");
  }
  return values;
}

function resolveSelectValue(field, suppliedValue) {
  const value = String(suppliedValue);
  const html = String(field?.editHtml ?? "");
  for (const select of html.matchAll(
    /<select\b([^>]*)>([\s\S]*?)<\/select>/gi
  )) {
    const selectAttributes = attributes(select[1]);
    if (
      selectAttributes.name !== field.id ||
      selectAttributes.disabled !== undefined
    )
      continue;
    const options = [
      ...select[2].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi),
    ]
      .map((match) => ({
        attrs: attributes(match[1]),
        label: decodeHtml(match[2].replace(/<[^>]*>/g, ""))
          .replace(/[\t\n\f\r ]+/g, " ")
          .trim(),
      }))
      .filter(
        (option) =>
          option.attrs.disabled === undefined && option.attrs.value !== undefined
      );
    const exactValues = options.filter((option) => option.attrs.value === value);
    if (exactValues.length) return value;
    const matchedValues = [
      ...new Set(
        options
          .filter((option) => option.label === value.replace(/[\t\n\f\r ]+/g, " ").trim())
          .map((option) => option.attrs.value)
      ),
    ];
    if (matchedValues.length === 1) return matchedValues[0];
    throw new JiraProtocolError(
      `select field ${field.id} has an unsupported or ambiguous value`
    );
  }
  return value;
}

export function createFormFromModel(model, suppliedFields) {
  if (!model || typeof model !== "object" || !Array.isArray(model.fields))
    throw new JiraProtocolError("Quick Create returned an unsupported model");
  if (typeof model.atl_token !== "string" || !model.atl_token)
    throw new JiraProtocolError("Quick Create model has no atl_token");
  if (typeof model.formToken !== "string" || !model.formToken)
    throw new JiraProtocolError("Quick Create model has no formToken");

  const values = new Map();
  for (const field of model.fields) {
    for (const [name, entries] of defaultsFromEditHtml(
      field?.editHtml
    ).entries()) {
      if (!values.has(name)) values.set(name, []);
      values.get(name).push(...entries);
    }
  }
  for (const [name, value] of Object.entries(suppliedFields)) {
    const field = model.fields.find((item) => item?.id === name);
    values.set(name, [field ? resolveSelectValue(field, value) : String(value)]);
  }
  values.set("atl_token", [model.atl_token]);
  values.set("formToken", [model.formToken]);
  values.set("pid", ["10848"]);
  values.set("issuetype", ["10000"]);

  const missing = model.fields
    .filter((field) => field?.required)
    .filter((field) => {
      const parameterName =
        field.id === "project"
          ? "pid"
          : field.id === "issuetype"
            ? "issuetype"
            : field.id;
      const candidates = values.get(parameterName) ?? [];
      return !candidates.some((value) => String(value).trim() !== "");
    })
    .map((field) => ({ id: field.id, label: field.label ?? field.id }));

  const form = new URLSearchParams();
  for (const [name, entries] of values)
    for (const value of entries) form.append(name, value);
  const safeForm = [...form.entries()]
    .filter(([name]) => !/^(?:atl_?token|formtoken)$/i.test(name))
    .map(([name, value]) => [name, value])
    .sort(([leftName, leftValue], [rightName, rightValue]) =>
      leftName === rightName
        ? leftValue.localeCompare(rightValue)
        : leftName.localeCompare(rightName)
    );
  const modelFields = model.fields
    .map((field) => ({
      id: String(field?.id ?? ""),
      label: String(field?.label ?? field?.id ?? ""),
      required: Boolean(field?.required),
    }))
    .sort((left, right) => left.id.localeCompare(right.id));
  return { form, missing, safeForm, modelFields };
}
