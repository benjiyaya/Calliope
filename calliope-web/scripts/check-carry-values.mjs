#!/usr/bin/env node
/**
 * Regression guard for stale node values surviving a workflow switch.
 *
 * Form values are keyed by nodeId, and a nodeId only means something inside one workflow.
 * Switching a clip from a video-finish workflow (3=FILM interpolation 2, 4=VSR scale 2.0,
 * 5=quality "ultra") to an H3 ref2v workflow (3=Width, 4=Height, 5=Seed) kept those values,
 * submitted a 2×2 frame with seed "ultra", and auto-save persisted it on the clip, so the
 * next Generate failed the same way.
 *
 * Fixtures are the input schemas of those two workflows (media inputs omitted). The helper is bundled
 * with esbuild so the TypeScript under test is the code the app ships.
 *
 * Run with `npm test` (or `node scripts/check-carry-values.mjs`).
 */
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENTRY = join(HERE, "..", "src", "lib", "comfy", "carryValues.ts");

const out = await build({
  entryPoints: [ENTRY],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
});
const file = join(
  mkdtempSync(join(tmpdir(), "carry-values-")),
  "carryValues.mjs",
);
writeFileSync(file, out.outputFiles[0].text);
const { carryValuesAcrossWorkflows, sanitizeWorkflowValues, MIN_DIMENSION_PX } =
  await import(pathToFileURL(file).href);

const H3_REF = [
  {
    nodeId: "2",
    label: "Prompt",
    role: "prompt",
    kind: "textarea",
    defaultValue: "",
  },
  {
    nodeId: "3",
    label: "Width",
    role: "width",
    kind: "number",
    defaultValue: 1280,
  },
  {
    nodeId: "4",
    label: "Height",
    role: "height",
    kind: "number",
    defaultValue: 720,
  },
  { nodeId: "5", label: "Seed", role: "seed", kind: "number", defaultValue: 7 },
  {
    nodeId: "6",
    label: "Duration seconds",
    role: "duration",
    kind: "number",
    defaultValue: 6.0,
  },
];
const VIDEO_FINISH = [
  {
    nodeId: "2",
    label: "Clip to finish",
    role: "video",
    kind: "video",
    defaultValue: null,
  },
  {
    nodeId: "3",
    label: "FILM interpolation 1-4 (1 = skip)",
    role: "interpolation",
    kind: "number",
    defaultValue: 2,
  },
  {
    nodeId: "4",
    label: "RTX VSR scale 1.0-4.0 (1.0 = skip)",
    role: "scale",
    kind: "number",
    defaultValue: 2.0,
  },
  {
    nodeId: "5",
    label: "VSR quality low|medium|high|ultra",
    role: "quality",
    kind: "textarea",
    defaultValue: "ultra",
  },
];

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failures++;
    console.error(
      `  FAIL ${name}\n       ${err.message.split("\n").join("\n       ")}`,
    );
  }
}

console.log("carry values across a workflow switch");
check("finish-workflow values do not survive a switch to H3 ref", () => {
  const finish = { 2: "clip_0007.mp4", 3: 2, 4: 2, 5: "ultra" };
  assert.deepEqual(
    carryValuesAcrossWorkflows(finish, VIDEO_FINISH, H3_REF),
    {},
  );
});
check("same workflow keeps every schema value", () => {
  const v = { 2: "a fox", 3: 1280, 4: 720, 5: 42, 6: 6 };
  assert.deepEqual(carryValuesAcrossWorkflows(v, H3_REF, H3_REF), v);
});
check(
  "a node with the same role and kind carries (the prompt survives)",
  () => {
    const other = [
      { nodeId: "2", label: "Prompt", role: "prompt", kind: "textarea" },
    ];
    assert.deepEqual(
      carryValuesAcrossWorkflows({ 2: "a fox", 9: 1 }, other, H3_REF),
      { 2: "a fox" },
    );
  },
);
check("empty values and unknown schemas carry nothing", () => {
  assert.deepEqual(carryValuesAcrossWorkflows({ 2: "" }, H3_REF, H3_REF), {});
  assert.deepEqual(
    carryValuesAcrossWorkflows({ 2: "x" }, undefined, H3_REF),
    {},
  );
});

console.log("sanitize saved values");
check(
  'heals a saved setup: 2×2 and seed "ultra" dropped, duration and refs kept',
  () => {
    const saved = {
      3: 2,
      4: 2,
      5: "ultra",
      6: 6,
      10: "/assets/18/image/ref1.png",
    };
    assert.deepEqual(sanitizeWorkflowValues(saved, H3_REF), {
      6: 6,
      10: "/assets/18/image/ref1.png",
    });
  },
);
check("valid values pass unchanged (numeric strings included)", () => {
  const v = { 2: "a fox", 3: 1280, 4: 720, 5: "42", 6: 6 };
  assert.deepEqual(sanitizeWorkflowValues(v, H3_REF), v);
});
check(
  `dimension floor is ${MIN_DIMENSION_PX} px (boundary kept, below dropped)`,
  () => {
    assert.deepEqual(
      sanitizeWorkflowValues(
        { 3: MIN_DIMENSION_PX, 4: MIN_DIMENSION_PX - 1 },
        H3_REF,
      ),
      { 3: MIN_DIMENSION_PX },
    );
  },
);
check("empty strings and keys outside the schema are left alone", () => {
  assert.deepEqual(sanitizeWorkflowValues({ 5: "", 99: "x" }, H3_REF), {
    5: "",
    99: "x",
  });
});

if (failures) {
  console.error(`\n${failures} carry-values check(s) failed.`);
  process.exit(1);
}
console.log("\ncarry-values checks passed.");
