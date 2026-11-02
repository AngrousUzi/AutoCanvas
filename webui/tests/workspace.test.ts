import { test } from "node:test";
import assert from "node:assert/strict";
import {
  queueLessons,
  groupArtifacts,
  slideFiles,
  isActive,
  timecode,
  transcriptLabel,
  transcriptRank,
} from "../src/workspace.ts";
import type { Lecture, Run, OutputFile } from "../src/workspace.ts";
const lecture: Lecture = { id: "2", course_id: "1", kind: "vod", name: "课堂" };
const run = (kind: string, status: string): Run => ({
  id: kind,
  kind,
  status,
  course_id: "1",
  lecture_id: "2",
  attempts: 1,
  created: 1,
  updated: 1,
});
test("completed, failed and cancelled work belongs in history, not the queue", () => {
  for (const status of ["succeeded", "failed", "cancelled"]) {
    assert.equal(
      queueLessons(
        [lecture],
        [run("vod_asr", status), run("vod_slides", status)],
      ).length,
      0,
    );
  }
});
test("running and pending steps are grouped under one lecture", () => {
  const items = queueLessons(
    [lecture],
    [run("vod_asr", "running"), run("vod_slides", "pending")],
  );
  assert.equal(items.length, 1);
  assert.equal(items[0].state, "running");
  assert.equal(items[0].steps.length, 2);
});
test("missing replay output remains schedulable without redoing completed output", () => {
  const items = queueLessons([lecture], [run("vod_asr", "succeeded")]);
  assert.equal(items[0].state, "ready");
  assert.equal(items[0].steps.filter((s) => !s.run).length, 1);
});
test("future live lectures are upcoming, ended lectures are not newly scheduled", () => {
  const live = {
    ...lecture,
    kind: "live",
    begin: "2030-01-02T12:00:00Z",
    end: "2030-01-02T13:00:00Z",
  };
  assert.equal(
    queueLessons([live], [], Date.parse("2030-01-01"))[0].state,
    "upcoming",
  );
  assert.equal(queueLessons([live], [], Date.parse("2030-01-03")).length, 0);
  assert.equal(
    queueLessons(
      [live],
      [run("live", "needs_login")],
      Date.parse("2030-01-03"),
    )[0].state,
    "needs_login",
  );
});
test("live and replay executions with the same id remain separate", () => {
  const live = { ...lecture, kind: "live", end: "2030-01-01" };
  const rows = queueLessons(
    [lecture, live],
    [
      run("live", "running"),
      run("vod_asr", "succeeded"),
      run("vod_slides", "succeeded"),
    ],
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].lecture.kind, "live");
});
const file = (path: string): OutputFile => ({
  path,
  name: path.split("/").pop()!,
  size: 100,
  modified: 1,
});
test("library merges live and replay artifacts but isolates samples and assignments", () => {
  const groups = groupArtifacts([
    file("outputs/1/2/vod_asr/transcript.json"),
    file("outputs/1/2/live/segments.jsonl"),
    file("outputs/1/2/vod_slides/result/frame_slide_001_00000s.jpg"),
    file("samples/uuid/1/2/vod_asr/transcript.json"),
    file("assignments/1/2/description.txt"),
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups.find((g) => !g.sample)?.files.length, 3);
  assert.equal(slideFiles(groups.find((g) => !g.sample)!).length, 1);
});
test("incomplete replacement files and backup copies never appear in gallery", () => {
  const groups = groupArtifacts([
    file("outputs/1/2/vod_slides/pending/frame_slide_001_00000s.jpg"),
    file("outputs/1/2/vod_slides/previous/frame_slide_001_00000s.jpg"),
    file("outputs/1/2/vod_asr/transcript.json.tmp"),
  ]);
  assert.equal(groups.length, 0);
});
test("gallery excludes contact sheet and orders numbered slides naturally", () => {
  const [group] = groupArtifacts([
    file("outputs/1/2/vod_slides/result/contact_sheet.jpg"),
    file("outputs/1/2/vod_slides/result/frame_slide_010_00050s.jpg"),
    file("outputs/1/2/vod_slides/result/frame_slide_002_00010s.jpg"),
  ]);
  assert.deepEqual(
    slideFiles(group).map((f) => f.name),
    ["frame_slide_002_00010s.jpg", "frame_slide_010_00050s.jpg"],
  );
});
test("status and time labels handle missing or invalid data", () => {
  assert.ok(isActive(run("live", "needs_login")));
  assert.ok(!isActive(run("live", "expired")));
  assert.equal(timecode(3661), "01:01:01");
  assert.equal(timecode(-10), "00:00:00");
});
test("transcript folders are labelled and ordered replay, platform, live", () => {
  const paths = [
    "outputs/1/2/live/segments.jsonl",
    "outputs/1/2/platform/transcript.json",
    "outputs/1/2/vod_asr/transcript.json",
  ].sort((a, b) => transcriptRank(a) - transcriptRank(b));
  assert.deepEqual(paths.map(transcriptLabel), [
    "回放转写",
    "平台原始转写",
    "直播转写",
  ]);
});
