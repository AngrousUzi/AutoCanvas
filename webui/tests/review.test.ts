import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildOccurrences,
  buildRows,
  defaultSource,
  normalizeSegments,
  occurrenceAt,
  offsetAtTime,
  readingLine,
  rowAtTime,
  sampleInterval,
  scrollForLine,
  timeAtOffset,
  transcriptSources,
} from "../src/review.ts";
import type { RowBox, TranscriptRow } from "../src/review.ts";
import { parseJsonLines } from "../src/workspace.ts";

const image = (n: number, seconds: number) => ({
  name: `frame_slide_${String(n).padStart(3, "0")}_${String(seconds).padStart(5, "0")}s.jpg`,
  path: `outputs/1/2/vod_slides/result/frame_slide_${n}.jpg`,
});
const images = [image(1, 0), image(2, 50), image(3, 100)];
const record = (n: number, time: number, frames?: number[]) => ({
  slide_number: n,
  time_seconds: time,
  image: images[n - 1].name,
  ...(frames ? { matched_sample_frames: frames } : {}),
});

test("slide timeline rebuilds returns from matched sample frames", () => {
  // Slide 1 frames 1-10, slide 2 frames 11-20, back to slide 1 frames 21-25,
  // slide 3 frames 26-30, then slide 2 again at 31.
  const manifest = [
    record(1, 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 21, 22, 23, 24, 25]),
    record(2, 50, [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 31]),
    record(3, 125, [26, 27, 28, 29, 30]),
  ];
  const runs = buildOccurrences(manifest, images);
  assert.deepEqual(
    runs.map((r) => [r.slide, r.start, r.end, r.returnOf]),
    [
      [0, 0, 50, null],
      [1, 50, 100, null],
      [0, 100, 125, 0],
      [2, 125, 150, null],
      [1, 150, 155, 1],
    ],
  );
});

test("frames matching no slide extend the previous occurrence", () => {
  const manifest = [record(1, 0, [1, 2, 3]), record(2, 50, [11, 12])];
  const runs = buildOccurrences(manifest, images);
  assert.deepEqual(
    runs.map((r) => [r.slide, r.start, r.end]),
    [
      [0, 0, 50],
      [1, 50, 60],
    ],
  );
  // Same slide on both sides of skipped frames stays one occurrence.
  const merged = buildOccurrences([record(1, 0, [1, 2, 6, 7])], images);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].end, 35);
});

test("sample interval is derived from the data, falling back to 5 seconds", () => {
  assert.equal(sampleInterval([record(2, 30, [11])]), 3);
  assert.equal(sampleInterval([record(1, 0, [1, 2])]), 5);
  assert.equal(sampleInterval([]), 5);
  const runs = buildOccurrences(
    [record(1, 0, [1, 2]), record(2, 6, [3, 4])],
    images,
  );
  assert.deepEqual(
    runs.map((r) => [r.start, r.end]),
    [
      [0, 6],
      [6, 12],
    ],
  );
});

test("slides without frame lists fall back to time, then file name", () => {
  const byTime = buildOccurrences(
    [record(2, 40), record(1, 10), record(3, 70)],
    images,
  );
  assert.deepEqual(
    byTime.map((r) => [r.slide, r.start]),
    [
      [0, 10],
      [1, 40],
      [2, 70],
    ],
  );
  const byName = buildOccurrences(undefined, [images[2], images[0], images[1]]);
  assert.deepEqual(
    byName.map((r) => r.start),
    [0, 50, 100],
  );
  assert.deepEqual(buildOccurrences([], []), []);
  const single = buildOccurrences([record(1, 0, [1, 2, 3])], [images[0]]);
  assert.deepEqual(
    single.map((r) => [r.start, r.end]),
    [[0, 15]],
  );
});

test("segments are validated, clamped and stably sorted", () => {
  const rows = normalizeSegments([
    { start: 30, end: 20, text: "b" },
    { start: 10, end: 12, text: "  " },
    { start: 5, end: 9, text: "a" },
    { start: 30, end: 40, text: "c" },
    { start: "x", text: "bad" },
    null,
    { start: 1, text: "no end" },
  ]);
  assert.deepEqual(
    rows.map((r) => [r.text, r.start, r.end]),
    [
      ["no end", 1, 1],
      ["a", 5, 9],
      ["b", 30, 30],
      ["c", 30, 40],
    ],
  );
});

test("JSON Lines parsing skips partly written lines", () => {
  const { rows, invalid } = parseJsonLines(
    '{"start":1,"end":2,"text":"a"}\n{"start":3,\n',
  );
  assert.equal(rows.length, 1);
  assert.equal(invalid, 1);
});

test("rows include gap rows for long silences, before the start and after the end", () => {
  const rows = buildRows(
    normalizeSegments([
      { start: 30, end: 40, text: "a" },
      { start: 45, end: 50, text: "b" },
      { start: 90, end: 95, text: "c" },
    ]),
    { until: 200 },
  );
  assert.deepEqual(
    rows.map((r) => [r.kind, r.t0, r.t1]),
    [
      ["gap", 0, 30],
      ["segment", 30, 40],
      ["segment", 45, 50],
      ["gap", 50, 90],
      ["segment", 90, 95],
      ["gap", 95, 200],
    ],
  );
  assert.equal(buildRows([], { until: 100 }).length, 0);
});

test("overlapping segments keep the effective spans monotonic", () => {
  const rows = buildRows(
    normalizeSegments([
      { start: 10, end: 50, text: "long" },
      { start: 20, end: 30, text: "inner" },
      { start: 25, end: 26, text: "tiny" },
      { start: 60, end: 61, text: "after" },
    ]),
  );
  for (let i = 0; i < rows.length; i++) {
    assert.ok(rows[i].t0 <= rows[i].t1);
    if (i) assert.ok(rows[i - 1].t1 <= rows[i].t0);
  }
  // The long segment is followed by a gap measured from its real end.
  assert.equal(rows.filter((r) => r.kind === "gap").length, 0);
});

test("time to occurrence uses binary search and clamps at both ends", () => {
  const runs = buildOccurrences(
    [record(1, 10, [3, 4, 9]), record(2, 25, [6, 7])],
    images,
  );
  assert.deepEqual(
    runs.map((r) => [r.slide, r.start]),
    [
      [0, 10],
      [1, 25],
      [0, 40],
    ],
  );
  assert.equal(occurrenceAt(runs, 0), 0);
  assert.equal(occurrenceAt(runs, 24.9), 0);
  assert.equal(occurrenceAt(runs, 25), 1);
  assert.equal(occurrenceAt(runs, 39), 1);
  assert.equal(occurrenceAt(runs, 1000), 2);
  assert.equal(occurrenceAt([], 10), -1);
});

const layout = (rows: TranscriptRow[], height = 40, margin = 0) => {
  let top = 0;
  return rows.map((r): RowBox => {
    const h = r.kind === "gap" ? 20 : height;
    const box = { top, height: h };
    top += h + margin;
    return box;
  });
};

test("reading position interpolates inside long segments and gaps", () => {
  const rows = buildRows(
    normalizeSegments([
      { start: 0, end: 118, text: "long" },
      { start: 150, end: 160, text: "next" },
    ]),
  );
  const boxes = layout(rows);
  assert.deepEqual(
    rows.map((r) => r.kind),
    ["segment", "gap", "segment"],
  );
  assert.equal(timeAtOffset(rows, boxes, 0), 0);
  assert.equal(timeAtOffset(rows, boxes, 20), 59);
  assert.equal(timeAtOffset(rows, boxes, 50), 134);
  assert.equal(timeAtOffset(rows, boxes, 1000), 160);
  assert.deepEqual(rowAtTime(rows, 59), { row: 0, fraction: 0.5 });
  assert.deepEqual(rowAtTime(rows, 134), { row: 1, fraction: 0.5 });
  assert.equal(offsetAtTime(rows, boxes, 134), 50);
});

test("between rows the next segment start is used, and lookups fall forward", () => {
  const rows = buildRows(
    normalizeSegments([
      { start: 0, end: 5, text: "a" },
      { start: 10, end: 15, text: "b" },
    ]),
  );
  const boxes = layout(rows, 40, 10);
  assert.equal(timeAtOffset(rows, boxes, 45), 10);
  assert.deepEqual(rowAtTime(rows, 7), { row: 1, fraction: 0 });
  assert.deepEqual(rowAtTime(rows, 99), { row: 1, fraction: 1 });
  assert.deepEqual(rowAtTime(rows, -5), { row: 0, fraction: 0 });
  assert.equal(timeAtOffset([], [], 10), 0);
});

test("offset to time is monotonic and round-trips through time to offset", () => {
  const rows = buildRows(
    normalizeSegments([
      { start: 3, end: 31, text: "a" },
      { start: 20, end: 25, text: "overlap" },
      { start: 37, end: 46, text: "b" },
      { start: 46, end: 46, text: "zero" },
      { start: 46, end: 60, text: "same start" },
      { start: 130, end: 248, text: "long" },
      { start: 300, end: 301, text: "end" },
    ]),
    { until: 400 },
  );
  const boxes = layout(rows, 37, 3);
  let previous = -1;
  const bottom = boxes[boxes.length - 1].top + boxes[boxes.length - 1].height;
  for (let y = -5; y <= bottom + 5; y += 0.5) {
    const t = timeAtOffset(rows, boxes, y);
    assert.ok(t >= previous, `time went back at ${y}: ${t} < ${previous}`);
    previous = t;
  }
  for (let t = 0; t <= 400; t += 0.25) {
    const y = offsetAtTime(rows, boxes, t);
    const back = timeAtOffset(rows, boxes, y);
    // Unless t falls in an untranscribed hole, the position maps back to t.
    if (rows.some((r) => r.t0 <= t && t < r.t1))
      assert.ok(Math.abs(back - t) < 1e-6, `${t} -> ${y} -> ${back}`);
    else assert.ok(back >= t);
  }
});

test("reading line covers the whole content and inverts", () => {
  for (const [viewport, max] of [
    [600, 5000],
    [600, 300],
    [600, 600],
    [600, 0],
  ]) {
    assert.equal(readingLine(0, viewport, max), 0);
    assert.equal(readingLine(max, viewport, max), max ? max + viewport : 0);
    let previous = -1;
    for (let s = 0; s <= max; s += 7) {
      const line = readingLine(s, viewport, max);
      assert.ok(line > previous || max === 0);
      previous = line;
      assert.ok(Math.abs(scrollForLine(line, viewport, max) - s) < 0.5);
    }
  }
  // In the middle of a long list the line sits a third down the pane.
  assert.equal(readingLine(2000, 600, 5000), 2200);
  assert.equal(scrollForLine(-10, 600, 5000), 0);
  assert.equal(scrollForLine(1e9, 600, 5000), 5000);
});

test("transcript sources prefer finished replay, then platform, never merge", () => {
  const key = "outputs/1/2";
  const f = (name: string, size = 100) => ({ path: `${key}/${name}`, size });
  const all = transcriptSources(key, [
    f("vod_asr/transcript.json", 2),
    f("vod_asr/segments.jsonl"),
    f("platform/transcript.json"),
    f("live/segments.jsonl"),
  ]);
  assert.deepEqual(
    all.map((s) => [s.key, s.partial]),
    [
      ["replay", true],
      ["platform", false],
      ["live", true],
    ],
  );
  assert.equal(defaultSource(all).key, "platform");
  const finished = transcriptSources(key, [
    f("platform/transcript.json"),
    f("vod_asr/transcript.json"),
    f("vod_asr/segments.jsonl"),
  ]);
  assert.equal(defaultSource(finished).path, `${key}/vod_asr/transcript.json`);
  const partialOnly = transcriptSources(key, [
    f("live/segments.jsonl"),
    f("vod_asr/segments.jsonl"),
  ]);
  assert.equal(defaultSource(partialOnly).key, "replay");
  assert.equal(defaultSource([]), undefined);
});
