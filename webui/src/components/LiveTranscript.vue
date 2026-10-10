<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { fileUrl } from "../api";
import { timecode } from "../workspace";
import type { Lecture } from "../workspace";
const props = defineProps<{ lecture: Lecture }>();
const scroller = ref<HTMLElement>();
const follow = ref(true);
const path = computed(
  () =>
    `outputs/${props.lecture.course_id}/${props.lecture.id}/${props.lecture.kind === "live" ? "live" : "vod_asr"}/segments.jsonl`,
);
const content = useQuery({
  queryKey: computed(() => ["live-transcript", path.value]),
  queryFn: async ({ signal }) => {
    const response = await fetch(fileUrl(path.value), {
      signal,
      cache: "no-store",
    });
    if (response.status === 404) return "";
    if (!response.ok) throw new Error("转写连接中断，正在自动重试");
    return response.text();
  },
  refetchInterval: 2000,
  retry: false,
  gcTime: 0,
});
const segments = computed<{ start: number; text: string }[]>(() =>
  (content.data.value || "").split("\n").flatMap((line) => {
    try {
      const row = JSON.parse(line);
      return typeof row.start === "number" &&
        typeof row.text === "string" &&
        row.text.trim()
        ? [row]
        : [];
    } catch {
      return [];
    }
  }),
);
async function latest() {
  follow.value = true;
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}
watch(
  segments,
  () => {
    if (follow.value) void latest();
  },
  { flush: "post" },
);
function onScroll() {
  const el = scroller.value;
  if (el) follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}
</script>
<template>
  <section class="live-transcript">
    <div class="transcript-head">
      <b>实时转写</b><span>{{ segments.length }} 段</span
      ><el-button link @click="latest">{{
        follow ? "跟随最新" : "回到最新 ↓"
      }}</el-button>
    </div>
    <p class="transcript-status" :class="{ error: content.isError.value }">
      {{
        content.isError.value
          ? "连接中断，正在重试；已接收内容保留。"
          : "每 2 秒更新 · 识别完成后逐段显示"
      }}
    </p>
    <div
      ref="scroller"
      class="transcript-scroll"
      tabindex="0"
      aria-label="课堂实时转写"
      @scroll="onScroll"
    >
      <div v-if="!segments.length" class="transcript-empty">
        {{
          content.isPending.value
            ? "正在连接课堂转写…"
            : "等待课堂转写，识别到的内容会显示在这里。"
        }}
      </div>
      <div
        v-for="(segment, index) in segments"
        :key="index"
        class="live-segment"
      >
        <time>{{ timecode(segment.start) }}</time>
        <p>{{ segment.text }}</p>
      </div>
    </div>
  </section>
</template>
<style scoped>
.live-transcript {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ochrelune-border);
  border-radius: var(--ochrelune-radius-panel);
  background: var(--ochrelune-surface);
  overflow: hidden;
}
.transcript-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--ochrelune-border);
}
.transcript-head span {
  margin-left: auto;
  font-size: 12px;
  color: var(--ochrelune-text-secondary);
}
.transcript-status {
  margin: 0;
  padding: 10px 16px;
  font-size: 12px;
  color: var(--ochrelune-text-secondary);
}
.transcript-status.error {
  color: var(--ochrelune-danger);
}
.transcript-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 18px 18px;
  overflow-wrap: anywhere;
}
.live-segment {
  padding: 12px 0;
  border-bottom: 1px solid var(--ochrelune-border);
}
time {
  color: var(--ochrelune-text-secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.live-segment p {
  margin: 5px 0 0;
  line-height: 1.85;
}
.transcript-empty {
  color: var(--ochrelune-text-secondary);
  padding: 40px 8px;
  line-height: 1.8;
}
</style>
