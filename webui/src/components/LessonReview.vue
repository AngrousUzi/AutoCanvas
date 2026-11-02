<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { fileUrl } from "../api";
import { parseJsonLines, timecode } from "../workspace";
import type { OutputFile } from "../workspace";
import {
  buildRows,
  defaultSource,
  gapHeight,
  lastAtOrBefore,
  normalizeSegments,
  occurrenceAt,
  offsetAtTime,
  readingLine,
  rowAtTime,
  scrollForLine,
  timeAtOffset,
  transcriptSources,
} from "../review";
import type { Occurrence, RowBox } from "../review";

const props = defineProps<{
  lessonKey: string;
  files: OutputFile[];
  slides: OutputFile[];
  occurrences: Occurrence[];
  /** Execution kinds currently running for this lesson (vod_asr, live, …). */
  running: string[];
}>();
const emit = defineEmits<{ open: [slide: number] }>();

const MAX_SIZE = 4 * 1024 * 1024;
const sources = computed(() => transcriptSources(props.lessonKey, props.files));
const sourceKey = ref("");
const source = computed(
  () =>
    sources.value.find((s) => s.key === sourceKey.value) ||
    defaultSource(sources.value),
);
const sourceFile = computed(() =>
  props.files.find((f) => f.path === source.value?.path),
);
const sourceRunning = computed(
  () =>
    !!source.value?.partial &&
    props.running.includes(source.value.key === "live" ? "live" : "vod_asr"),
);
// The key changes whenever /api/files reports a new size or mtime, so a growing
// segments.jsonl is refetched on the page's regular file-list poll.
const content = useQuery({
  queryKey: computed(() => [
    "review-transcript",
    sourceFile.value?.path,
    sourceFile.value?.modified,
    sourceFile.value?.size,
  ]),
  queryFn: async ({ signal }) => {
    const response = await fetch(fileUrl(sourceFile.value!.path), {
      signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error("转写读取失败");
    return response.text();
  },
  enabled: computed(
    () => !!sourceFile.value && sourceFile.value.size <= MAX_SIZE,
  ),
  // Keep showing the previous version of the same file while it refreshes.
  placeholderData: (previous, previousQuery) =>
    previousQuery?.queryKey[1] === sourceFile.value?.path
      ? previous
      : undefined,
});
const segments = computed(() => {
  const text = content.data.value || "";
  if (!text) return [];
  if (sourceFile.value?.path.endsWith(".jsonl"))
    return normalizeSegments(parseJsonLines(text).rows);
  try {
    return normalizeSegments(JSON.parse(text));
  } catch {
    return [];
  }
});
const timelineEnd = computed(() => {
  const last = props.occurrences[props.occurrences.length - 1];
  return last ? last.end : undefined;
});
const rows = computed(() =>
  buildRows(segments.value, { until: timelineEnd.value }),
);
const hasSlides = computed(() => props.occurrences.length > 0);

const currentTime = ref(0),
  activeRow = ref(-1),
  activeOcc = ref(0);
const occurrence = computed(() => props.occurrences[activeOcc.value]);

const narrowQuery =
  typeof window !== "undefined"
    ? window.matchMedia("(max-width: 760px)")
    : undefined;
const narrow = ref(!!narrowQuery?.matches);
const onNarrow = (event: MediaQueryListEvent) => (narrow.value = event.matches);

// ---- Scroll sync -----------------------------------------------------------
// The transcript owns the time axis. Only the pane the user last touched may
// drive the other; scroll events on the follower are our own and are ignored.
type Pane = "transcript" | "slides";
const transcriptPane = ref<HTMLElement>(),
  slidePane = ref<HTMLElement>();
const element = (pane: Pane) =>
  pane === "transcript" ? transcriptPane.value : slidePane.value;
let driver: Pane | null = null;
const lastInput: Record<Pane, number> = { transcript: 0, slides: 0 };
const expected: Record<Pane, number> = { transcript: NaN, slides: NaN };
let rowBoxes: RowBox[] = [],
  slideBoxes: RowBox[] = [],
  dirty = true,
  /** Viewport offset of the transcript point at currentTime. */
  anchor = 0,
  frame = 0,
  pending: Pane | null = null;

function input(pane: Pane) {
  const now = performance.now();
  // The transcript wins when both panes are being moved at once.
  if (
    pane === "slides" &&
    driver === "transcript" &&
    now - lastInput.transcript < 300
  )
    return;
  lastInput[pane] = now;
  expected[pane] = NaN;
  driver = pane;
}
function boxes(el: HTMLElement | undefined, selector: string) {
  return el
    ? Array.from(el.querySelectorAll<HTMLElement>(selector)).map((node) => ({
        top: node.offsetTop,
        height: node.offsetHeight,
      }))
    : [];
}
function measure() {
  if (!dirty) return;
  dirty = false;
  rowBoxes = boxes(transcriptPane.value, "[data-row]");
  slideBoxes = boxes(slidePane.value, "[data-occ]");
}
const maxScroll = (el: HTMLElement) =>
  Math.max(0, el.scrollHeight - el.clientHeight);
function setScroll(pane: Pane, top: number, smooth = false) {
  const el = element(pane);
  if (!el) return;
  const target = Math.round(Math.min(maxScroll(el), Math.max(0, top)));
  if (Math.abs(el.scrollTop - target) < 1) return;
  expected[pane] = target;
  if (smooth) el.scrollTo({ top: target, behavior: "smooth" });
  else el.scrollTop = target;
}
function onScroll(pane: Pane) {
  const el = element(pane);
  if (!el) return;
  if (Math.abs(el.scrollTop - expected[pane]) <= 1) {
    expected[pane] = NaN;
    return;
  }
  const now = performance.now();
  if (pane === "slides") {
    if (driver !== "slides") return;
    lastInput.slides = now; // keeps momentum scrolling attributed to the user
  } else {
    if (driver === "slides" && now - lastInput.slides < 1000) return;
    driver = "transcript";
  }
  if (pending !== "transcript") pending = pane;
  if (!frame)
    frame = requestAnimationFrame(() => {
      frame = 0;
      const which = pending;
      pending = null;
      if (which === "transcript") fromTranscript();
      else if (which === "slides") fromSlides();
    });
}
function placeSlides(index: number, smooth = false) {
  const el = slidePane.value,
    box = slideBoxes[index];
  if (!el || !box) return;
  const top = scrollForLine(
    box.top + box.height / 2,
    el.clientHeight,
    maxScroll(el),
  );
  setScroll(
    "slides",
    top,
    smooth && Math.abs(el.scrollTop - top) < el.clientHeight * 1.5,
  );
}
function selectOccurrenceAt(t: number, smooth: boolean) {
  const index = occurrenceAt(props.occurrences, t);
  if (index < 0 || index === activeOcc.value) return;
  activeOcc.value = index;
  placeSlides(index, smooth);
}
function fromTranscript() {
  const el = transcriptPane.value;
  if (!el || !rows.value.length) return;
  measure();
  const line = readingLine(el.scrollTop, el.clientHeight, maxScroll(el));
  anchor = line - el.scrollTop;
  currentTime.value = timeAtOffset(rows.value, rowBoxes, line);
  activeRow.value = Math.max(
    0,
    lastAtOrBefore(rowBoxes.length, (k) => rowBoxes[k].top, line),
  );
  selectOccurrenceAt(currentTime.value, true);
}
function fromSlides() {
  const el = slidePane.value;
  if (!el || !slideBoxes.length) return;
  measure();
  const line = readingLine(el.scrollTop, el.clientHeight, maxScroll(el));
  const index = Math.max(
    0,
    lastAtOrBefore(slideBoxes.length, (k) => slideBoxes[k].top, line),
  );
  if (index !== activeOcc.value) chooseOccurrence(index, false);
}
/** Put the transcript point for time t on the reading line. */
function placeTranscript(t: number, offset?: number) {
  const el = transcriptPane.value;
  if (!el || !rows.value.length) return;
  measure();
  const y = offsetAtTime(rows.value, rowBoxes, t);
  setScroll(
    "transcript",
    offset === undefined
      ? scrollForLine(y, el.clientHeight, maxScroll(el))
      : y - offset,
  );
  anchor = y - el.scrollTop;
  activeRow.value = rowAtTime(rows.value, t).row;
}
function chooseOccurrence(index: number, scrollSlides = true) {
  const item = props.occurrences[index];
  if (!item) return;
  activeOcc.value = index;
  currentTime.value = item.start;
  placeTranscript(item.start);
  // Instant, so the slide pane emits a single scroll event we can recognise.
  if (scrollSlides) placeSlides(index);
}
function openOccurrence(index: number) {
  chooseOccurrence(index);
  emit("open", props.occurrences[index].slide);
}
function chooseRow(index: number) {
  const row = rows.value[index];
  if (!row) return;
  measure();
  const el = transcriptPane.value,
    box = rowBoxes[index];
  currentTime.value = row.start;
  if (el && box)
    setScroll(
      "transcript",
      scrollForLine(box.top, el.clientHeight, maxScroll(el)),
    );
  if (el && box) anchor = box.top - el.scrollTop;
  activeRow.value = index;
  selectOccurrenceAt(row.start, true);
}
let paneSize = "";
/** The row under the reading line before a content refresh, and where it was on screen. */
let kept: { kind: string; start: number; text: string; top: number } | null =
  null;
watch(
  rows,
  (_, previous) => {
    const el = transcriptPane.value,
      row = previous?.[activeRow.value];
    measure();
    const box = rowBoxes[activeRow.value];
    kept =
      el && row && box
        ? {
            kind: row.kind,
            start: row.start,
            text: row.text,
            top: box.top - el.scrollTop,
          }
        : null;
  },
  { flush: "pre" },
);
function keptRow() {
  if (!kept) return -1;
  const target = kept;
  let i = lastAtOrBefore(
    rows.value.length,
    (k) => rows.value[k].start,
    target.start,
  );
  for (; i >= 0 && rows.value[i].start === target.start; i--)
    if (
      rows.value[i].kind === target.kind &&
      rows.value[i].text === target.text
    )
      return i;
  return -1;
}
/**
 * Content changed (rows refreshed, slides loaded): keep the text the user was
 * reading at the same place on screen, then take the time from the reading
 * line so the next scroll continues from exactly there.
 * Pane resized: the reading line itself moved, so put the current time back on it.
 */
function relayout() {
  dirty = true;
  measure();
  const el = transcriptPane.value;
  const size = el ? `${el.clientWidth}x${el.clientHeight}` : "";
  const resized = size !== paneSize;
  paneSize = size;
  const same = resized ? -1 : keptRow(),
    keptTop = kept?.top ?? 0;
  kept = null;
  if (el && rows.value.length) {
    if (same >= 0 && rowBoxes[same])
      setScroll("transcript", rowBoxes[same].top - keptTop);
    else placeTranscript(currentTime.value, resized ? undefined : anchor);
    if (!resized) {
      const line = readingLine(el.scrollTop, el.clientHeight, maxScroll(el));
      anchor = line - el.scrollTop;
      currentTime.value = timeAtOffset(rows.value, rowBoxes, line);
      activeRow.value = Math.max(
        0,
        lastAtOrBefore(rowBoxes.length, (k) => rowBoxes[k].top, line),
      );
    }
    const index = occurrenceAt(props.occurrences, currentTime.value);
    if (index >= 0) activeOcc.value = index;
  } else {
    activeRow.value = -1;
    if (activeOcc.value >= props.occurrences.length)
      activeOcc.value = Math.max(0, props.occurrences.length - 1);
  }
  placeSlides(activeOcc.value);
}
watch(
  [rows, () => props.occurrences, narrow],
  () => {
    dirty = true;
    void nextTick(relayout);
  },
  { flush: "post" },
);
let observer: ResizeObserver | undefined,
  resizeFrame = 0;
function observe() {
  observer?.disconnect();
  for (const el of [transcriptPane.value, slidePane.value])
    if (el) {
      observer?.observe(el);
      if (el.firstElementChild) observer?.observe(el.firstElementChild);
    }
}
watch([transcriptPane, slidePane], observe, { flush: "post" });
onMounted(() => {
  narrowQuery?.addEventListener("change", onNarrow);
  observer = new ResizeObserver(() => {
    dirty = true;
    if (!resizeFrame)
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = 0;
        relayout();
      });
  });
  observe();
  relayout();
});
onUnmounted(() => {
  narrowQuery?.removeEventListener("change", onNarrow);
  observer?.disconnect();
  cancelAnimationFrame(frame);
  cancelAnimationFrame(resizeFrame);
});
function chooseSource(key: string) {
  sourceKey.value = key;
}
const position = computed(() =>
  occurrence.value
    ? `第 ${occurrence.value.slide + 1} / ${props.slides.length} 张`
    : "",
);
</script>
<template>
  <div v-if="!hasSlides && !sources.length" class="empty-state">
    <div class="empty-symbol">▤</div>
    <h3>这节课还不能回顾</h3>
    <p>画面抽取或转写完成后，会在这里按时间对照显示。</p>
  </div>
  <div v-else class="review" :class="{ narrow }">
    <div class="review-head">
      <div class="review-clock">
        <time>{{ timecode(currentTime) }}</time
        ><span v-if="position">{{ position }}</span>
      </div>
      <div
        v-if="sources.length"
        class="segmented"
        role="radiogroup"
        aria-label="转写来源"
      >
        <button
          v-for="item in sources"
          :key="item.key"
          role="radio"
          :aria-checked="source?.key === item.key"
          :class="{ selected: source?.key === item.key }"
          @click="chooseSource(item.key)"
        >
          {{ item.label }}
        </button>
      </div>
    </div>
    <p v-if="source?.key === 'live'" class="review-note">
      直播转写按课程开始时间计时，可能与回放画面错位。
    </p>
    <p v-if="source?.partial && sourceRunning" class="review-note">
      转写生成中，产物刷新时自动追加，阅读位置保持不变。
    </p>
    <p v-else-if="source?.partial" class="review-note">这份转写尚未完成。</p>
    <p v-if="!hasSlides" class="review-note">画面抽取完成后会显示在左侧。</p>
    <p v-if="!sources.length" class="review-note">
      这节课还没有转写，仅显示画面。
    </p>
    <div
      class="review-body"
      :class="{ 'no-slides': !hasSlides, 'no-transcript': !sources.length }"
    >
      <div v-if="hasSlides && narrow && occurrence" class="review-strip">
        <button
          class="slide-thumbnail"
          :aria-label="'放大第 ' + (occurrence.slide + 1) + ' 张画面'"
          @click="emit('open', occurrence.slide)"
        >
          <img
            :src="fileUrl(slides[occurrence.slide].path)"
            :alt="'第 ' + (occurrence.slide + 1) + ' 张课堂画面'"
          />
        </button>
        <div class="review-strip-bar">
          <el-button
            size="small"
            :disabled="activeOcc === 0"
            @click="chooseOccurrence(activeOcc - 1)"
            >←</el-button
          >
          <span
            ><time>{{ timecode(occurrence.start) }}</time>
            <em v-if="occurrence.returnOf !== null"
              >回到第 {{ occurrence.slide + 1 }} 张</em
            ></span
          >
          <el-button
            size="small"
            :disabled="activeOcc === occurrences.length - 1"
            @click="chooseOccurrence(activeOcc + 1)"
            >→</el-button
          >
        </div>
      </div>
      <div
        v-if="hasSlides && !narrow"
        ref="slidePane"
        class="review-pane review-slides"
        tabindex="0"
        aria-label="课堂画面，随转写滚动"
        @scroll.passive="onScroll('slides')"
        @wheel.passive="input('slides')"
        @touchstart.passive="input('slides')"
        @pointerdown="input('slides')"
        @keydown="input('slides')"
      >
        <div class="review-list">
          <div
            v-for="(item, i) in occurrences"
            :key="i"
            v-memo="[item, i === activeOcc]"
            data-occ
            class="slide-card review-slide"
            :class="{ active: i === activeOcc }"
          >
            <button
              class="slide-thumbnail"
              :aria-label="'放大第 ' + (item.slide + 1) + ' 张画面'"
              @click="openOccurrence(i)"
            >
              <img
                :src="fileUrl(slides[item.slide].path)"
                :alt="'第 ' + (item.slide + 1) + ' 张课堂画面'"
                loading="lazy"
              /><span>↗</span>
            </button>
            <button class="slide-caption" @click="chooseOccurrence(i)">
              <b>{{ String(item.slide + 1).padStart(2, "0") }}</b
              ><em v-if="item.returnOf !== null"
                >回到第 {{ item.slide + 1 }} 张</em
              ><time>{{ timecode(item.start) }}</time>
            </button>
          </div>
        </div>
      </div>
      <div
        v-if="sources.length"
        ref="transcriptPane"
        class="review-pane review-transcript"
        tabindex="0"
        aria-label="课堂转写，滚动时画面跟随"
        @scroll.passive="onScroll('transcript')"
        @wheel.passive="input('transcript')"
        @touchstart.passive="input('transcript')"
        @pointerdown="input('transcript')"
        @keydown="input('transcript')"
      >
        <div class="review-list">
          <div
            v-if="sourceFile && sourceFile.size > MAX_SIZE"
            class="empty-state"
          >
            <p>这份转写超过 4 MB，请在“课堂转写”中下载阅读。</p>
          </div>
          <div v-else-if="content.isError.value" class="empty-state">
            <p>暂时无法读取转写。</p>
            <el-button @click="content.refetch()">重新加载</el-button>
          </div>
          <div v-else-if="content.isPending.value" class="empty-state">
            正在打开转写…
          </div>
          <div v-else-if="!rows.length" class="empty-state">
            <p>这份转写暂无文字内容。</p>
          </div>
          <div
            v-for="(row, i) in rows"
            :key="row.kind + row.start + ':' + i"
            v-memo="[
              row,
              i === activeRow,
              i === rows.length - 1 && sourceRunning,
            ]"
            data-row
            :class="[
              row.kind === 'gap' ? 'review-gap' : 'review-segment',
              { active: i === activeRow },
            ]"
            :style="
              row.kind === 'gap'
                ? { height: gapHeight(row.end - row.start) + 'px' }
                : undefined
            "
          >
            <span v-if="row.kind === 'gap'">{{
              i === rows.length - 1 && sourceRunning
                ? "后续转写生成中"
                : `此段约 ${Math.round(row.end - row.start)}s 无转写`
            }}</span>
            <template v-else>
              <button
                class="review-time"
                :aria-label="'跳到 ' + timecode(row.start)"
                @click="chooseRow(i)"
              >
                {{ timecode(row.start) }}
              </button>
              <p>{{ row.text }}</p>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.review {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.review-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.review-clock {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-variant-numeric: tabular-nums;
}
.review-clock time {
  font-size: var(--app-section-size);
  color: var(--ochrelune-text);
}
.review-clock span {
  font-size: var(--app-meta-size);
  color: var(--ochrelune-text-muted);
}
.review-head .segmented button {
  padding: 7px 10px;
}
.review-note {
  margin: 0;
  font-size: var(--app-meta-size);
  color: var(--ochrelune-text-muted);
}
.review-body {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
  gap: 16px;
  height: max(460px, calc(100vh - 260px));
  min-width: 0;
}
.review-body.no-slides,
.review-body.no-transcript {
  grid-template-columns: minmax(0, 1fr);
}
.review-pane {
  position: relative;
  overflow-y: auto;
  overflow-x: hidden;
  overscroll-behavior: contain;
  overflow-anchor: none;
  min-height: 0;
  border: 1px solid var(--ochrelune-border);
  border-radius: var(--ochrelune-radius-panel);
  background: var(--ochrelune-surface);
}
.review-pane:focus-visible {
  outline: 2px solid var(--ochrelune-focus);
  outline-offset: 2px;
}
.review-slides .review-list {
  display: grid;
  gap: 12px;
  padding: 12px;
}
.no-transcript .review-slides .review-list {
  max-width: 560px;
  margin: 0 auto;
}
.review-slide {
  cursor: default;
}
.review-slide.active {
  border-color: var(--ochrelune-accent);
  box-shadow: 0 0 0 1px var(--ochrelune-accent);
}
.review-slide .slide-thumbnail,
.review-strip .slide-thumbnail {
  width: 100%;
  border: 0;
  padding: 0;
  cursor: zoom-in;
}
.review-slide .slide-thumbnail:hover > span {
  opacity: 1;
}
.review-slide .slide-caption {
  width: 100%;
  border: 0;
  background: none;
  cursor: pointer;
  align-items: center;
  gap: 8px;
}
.review-slide .slide-caption time {
  margin-left: auto;
}
.review-slide em,
.review-strip em {
  font-style: normal;
  font-size: var(--app-meta-size);
  color: var(--ochrelune-warning);
  background: var(--ochrelune-warning-bg);
  padding: 2px 6px;
  border-radius: 3px;
}
.review-transcript .review-list {
  padding: 4px 16px;
}
.review-segment {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 12px;
  padding: 12px 8px;
  border-bottom: 1px solid var(--ochrelune-border);
  border-left: 2px solid transparent;
}
.review-segment.active {
  border-left-color: var(--ochrelune-accent);
  background: var(--ochrelune-surface-subtle);
}
.review-time {
  align-self: start;
  border: 0;
  background: none;
  padding: 4px 0 0;
  text-align: left;
  font-size: var(--app-meta-size);
  color: var(--ochrelune-text-muted);
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}
.review-time:hover {
  color: var(--ochrelune-accent);
}
.review-segment p {
  margin: 0;
  font-size: 14px;
  line-height: 1.9;
  color: var(--ochrelune-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.review-gap {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  padding: 0 8px 0 92px;
  border-bottom: 1px dashed var(--ochrelune-border);
  border-left: 2px solid transparent;
  font-size: var(--app-meta-size);
  color: var(--ochrelune-text-muted);
  background: repeating-linear-gradient(
    -45deg,
    transparent 0 6px,
    var(--ochrelune-surface-subtle) 6px 12px
  );
}
.review-gap.active {
  border-left-color: var(--ochrelune-accent);
}
.review-strip {
  display: grid;
  gap: 8px;
}
.review-strip .slide-thumbnail {
  max-height: 30vh;
  aspect-ratio: 16/10;
  border-radius: var(--ochrelune-radius-panel);
  overflow: hidden;
}
.review-strip-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--app-meta-size);
  color: var(--ochrelune-text-muted);
  font-variant-numeric: tabular-nums;
}
.review-strip-bar span {
  display: flex;
  gap: 8px;
  align-items: center;
}
.review.narrow .review-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: max(420px, calc(100svh - 80px));
}
.review.narrow .review-transcript {
  flex: 1;
}
@media (max-width: 760px) {
  .review-segment {
    grid-template-columns: 60px minmax(0, 1fr);
    gap: 8px;
    padding: 10px 4px;
  }
  .review-transcript .review-list {
    padding: 2px 8px;
  }
  .review-gap {
    padding-left: 68px;
  }
}
</style>
