<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { Lecture } from "../workspace";
const props = defineProps<{ lecture: Lecture; eager?: boolean }>();
const element = ref<HTMLElement>();
const visible = ref(!!props.eager);
let observer: IntersectionObserver | undefined;
onMounted(() => {
  observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      visible.value = true;
      observer?.disconnect();
    }
  });
  if (element.value) observer.observe(element.value);
});
onUnmounted(() => observer?.disconnect());
const streams = useQuery<{ view: string; url: string }[]>({
  queryKey: computed(() => [
    "lecture-streams",
    props.lecture.course_id,
    props.lecture.kind,
    props.lecture.id,
  ]),
  queryFn: () =>
    api(
      "/api/sources?" +
        new URLSearchParams({
          course_id: props.lecture.course_id,
          kind: props.lecture.kind,
          lecture_id: props.lecture.id,
          show_urls: "1",
        }),
    ),
  enabled: visible,
  retry: false,
  staleTime: 60000,
  gcTime: 0,
  refetchOnWindowFocus: false,
});
const links = computed(() =>
  (streams.data.value || []).filter((row) => {
    try {
      return ["http:", "https:"].includes(new URL(row.url).protocol);
    } catch {
      return false;
    }
  }),
);
async function copy(url: string) {
  try {
    await navigator.clipboard.writeText(url);
    ElMessage.success("已复制视频流地址");
  } catch {
    ElMessage.info("请长按或右键地址，选择复制链接。");
  }
}
</script>
<template>
  <slot :links="links" :refresh="streams.refetch" />
  <section ref="element" class="lecture-streams" aria-label="课次视频流">
    <div class="stream-heading">
      <b>视频流 · {{ lecture.kind === "live" ? "直播" : "回放" }}</b>
      <el-button
        link
        :loading="streams.isFetching.value"
        @click="streams.refetch()"
        >刷新地址</el-button
      >
    </div>
    <p v-if="streams.isPending.value" class="muted">正在获取各镜头地址…</p>
    <p v-else-if="streams.isError.value" class="stream-error">
      地址获取失败：{{ streams.error.value?.message }}。可点击刷新重试。
    </p>
    <template v-else>
      <div
        v-for="(source, index) in links"
        :key="source.view + ':' + index"
        class="stream-row"
      >
        <span class="stream-label"
          >镜头 {{ index + 1
          }}<small v-if="source.view && source.view !== 'unknown'"
            >画面编号 {{ source.view }}</small
          ></span
        >
        <a
          :href="source.url"
          :title="source.url"
          target="_blank"
          rel="noopener noreferrer"
          referrerpolicy="no-referrer"
          :aria-label="'打开镜头 ' + (index + 1) + ' 视频流'"
          >{{ source.url }}</a
        >
        <el-button link @click="copy(source.url)">复制</el-button>
      </div>
      <p v-if="!links.length" class="muted">
        暂未返回视频流，开播后可刷新获取。
      </p>
      <p v-else class="muted stream-note">
        {{
          links.length === 1 ? "当前仅返回一个镜头。" : ""
        }}地址可能过期，打不开时请刷新；浏览器不支持播放时，可复制到 VLC
        等播放器。
      </p>
    </template>
  </section>
</template>
<style scoped>
.lecture-streams {
  min-width: 0;
  margin: 18px 0;
  padding: 14px 16px;
  border: 1px solid var(--ochrelune-border);
  border-radius: var(--ochrelune-radius-panel);
  background: var(--ochrelune-surface-subtle);
}
.stream-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.stream-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 12px;
  min-width: 0;
}
.stream-label {
  flex: 0 0 82px;
  font-size: 14px;
}
.stream-label small {
  display: block;
  color: var(--ochrelune-text-secondary);
  font-size: 11px;
}
.stream-row a {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.6;
}
.stream-note {
  font-size: 12px;
  margin-bottom: 0;
}
.stream-error {
  color: var(--ochrelune-danger);
  font-size: 13px;
}
@media (max-width: 600px) {
  .stream-row {
    flex-wrap: wrap;
    gap: 8px;
  }
  .stream-row a {
    flex-basis: calc(100% - 100px);
  }
}
</style>
