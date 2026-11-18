import assert from "node:assert/strict";
import test from "node:test";
import { createFormFromModel } from "../../scripts/jira_protocol/html-form.mjs";

const model = {
  atl_token: "fixture",
  formToken: "fixture",
  fields: [
    {
      id: "customfield_10481",
      required: true,
      editHtml: '<select name="customfield_10481"><option value="">None</option><option value="58851">SERVICE</option></select>',
    },
    {
      id: "customfield_12100",
      required: true,
      editHtml: '<select name="customfield_12100"><option value="27508">Технический долг / развитие</option></select>',
    },
  ],
};
test("resolves Jira labels to server option values", () => {
  const { form } = createFormFromModel(model, {
    customfield_10481: "SERVICE",
    customfield_12100: "Технический долг / развитие",
  });
  assert.equal(form.get("customfield_10481"), "58851");
  assert.equal(form.get("customfield_12100"), "27508");
});
test("accepts an explicit option id", () => {
  const { form } = createFormFromModel(model, { customfield_10481: "58851" });
  assert.equal(form.get("customfield_10481"), "58851");
});
test("matches option labels with browser-normalized whitespace", () => {
  const spaced = structuredClone(model);
  spaced.fields[1].editHtml = '<select name="customfield_12100"><option value="27508">Технический  долг\n / развитие</option></select>';
  const { form } = createFormFromModel(spaced, {
    customfield_12100: "Технический долг / развитие",
  });
  assert.equal(form.get("customfield_12100"), "27508");
});
test("rejects unknown and ambiguous labels", () => {
  assert.throws(
    () => createFormFromModel(model, { customfield_10481: "unknown" }),
    /unsupported or ambiguous/
  );
  const duplicate = structuredClone(model);
  duplicate.fields[0].editHtml = '<select name="customfield_10481"><option value="1">SERVICE</option><option value="2">SERVICE</option></select>';
  assert.throws(
    () => createFormFromModel(duplicate, { customfield_10481: "SERVICE" }),
    /unsupported or ambiguous/
  );
});
