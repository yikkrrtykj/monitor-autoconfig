const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const context = { window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root, "bigscreen", "pages.js"), "utf8"), context);

const layouts = context.window.BIGSCREEN_TEAM_LAYOUTS;
const pages = context.window.BIGSCREEN_PAGES;
const page233 = pages.find((page) => page.id === "tournament-64-233");

assert.deepStrictEqual(
  Array.from(pages)
    .filter((page) => layouts.configurableLayoutIds.includes(page.id))
    .map((page) => [page.id, Array.from(page.groups, (group) => group.length)]),
  [
    ["tournament-64-2layer", [8, 8]],
    ["tournament-64-233", [6, 6, 4]],
    ["tournament-64-332", [4, 6, 6]]
  ]
);

assert.deepStrictEqual(
  Array.from(layouts.defaultTeamOrder(page233)),
  [11, 12, 13, 14, 15, 16, 5, 6, 7, 8, 9, 10, 1, 2, 3, 4]
);

const eventOrder = [14, 15, 16, 6, 7, 8, 11, 12, 13, 3, 4, 5, 9, 10, 1, 2];
const configured = layouts.applyTeamOrder(page233, JSON.stringify({
  "tournament-64-233": eventOrder
}));
assert.deepStrictEqual(
  Array.from(configured.groups, (group) => Array.from(group)),
  [
    [14, 15, 16, 6, 7, 8],
    [11, 12, 13, 3, 4, 5],
    [9, 10, 1, 2]
  ]
);
assert.strictEqual(configured.trendMode, "groups");

const duplicateOrder = [...eventOrder];
duplicateOrder[15] = 1;
assert.deepStrictEqual(
  Array.from(layouts.teamOrderForPage(page233, { "tournament-64-233": duplicateOrder })),
  [11, 12, 13, 14, 15, 16, 5, 6, 7, 8, 9, 10, 1, 2, 3, 4]
);

console.log("bigscreen team layout tests passed");
