<script setup lang="ts">
import { computed } from "vue";
import { useApiQuery } from "../queries";
import { fileUrl } from "../api";
import { date } from "../format";
import { timecode } from "../workspace";
import type { Lecture } from "../workspace";
import MaterialIcon from "./MaterialIcon.vue";
const props = defineProps<{ lecture: Lecture }>();
const attendance = useApiQuery("attendance", "/api/attendance", 1500);
const feed = useApiQuery("classroom-events", "/api/classroom-events", 10000);
interface CodeRecord {
  id: string;
  course_id: string;
  lecture_id: string;
  at: number;
  status: string;
  history?: string;
  image?: string;
  message?: string;
}
interface KeywordEvent {
  id: string;
  course_id: string;
  lecture_id: string;
  source: string;
  type: string;
  start: number;
  keyword: string;
  text: string;
}
const inLesson = (row: { course_id: string; lecture_id: string }) =>
  row.course_id === props.lecture.course_id &&
  row.lecture_id === props.lecture.id;
const codes = computed<CodeRecord[]>(() =>
  [
    ...new Map<string, CodeRecord>(
      (attendance.data.value?.records || [])
        .filter(inLesson)
        .map((row: CodeRecord) => [row.id, row] as const),
    ).values(),
  ].sort((a, b) => b.at - a.at),
);
const keywords = computed<KeywordEvent[]>(() =>
  [
    ...new Map<string, KeywordEvent>(
      (feed.data.value?.events || [])
        .filter(
          (row: KeywordEvent) =>
            inLesson(row) && row.source === "live" && row.type === "keyword",
        )
        .map((row: KeywordEvent) => [row.id, row] as const),
    ).values(),
  ].sort((a, b) => b.start - a.start),
);
const total = computed(() => codes.value.length + keywords.value.length);
const loading = computed(
  () => attendance.isPending.value || feed.isPending.value,
);
const failed = computed(() => attendance.isError.value || feed.isError.value);
const names: Record<string, string> = {
  submitting: "正在签到",
  succeeded: "接口确认成功",
  expired: "码已失效",
  needs_login: "等待重新认证",
  needs_action: "需要现场操作",
  rejected: "学校拒绝",
  unknown: "结果不确定，请核对",
};
const enabled = computed(() => {
  const config = attendance.data.value?.config;
  return (
    config?.enabled &&
    (!config.course_ids?.length ||
      config.course_ids.includes(props.lecture.course_id)) &&
    !attendance.data.value?.automation_paused
  );
});
</script>
<template>
  <details class="lesson-attendance">
    <summary>
      <MaterialIcon name="qr" />
      <b>本节课签到</b>
      <span
        class="attendance-count"
        :aria-label="`已读取 ${total} 条签到线索`"
        >{{ loading && !total ? "…" : total }}</span
      >
      <span class="attendance-summary">{{
        failed
          ? "部分数据加载失败"
          : loading
            ? "正在读取…"
            : `签到二维码 ${codes.length} · 点名 / 签到关键词 ${keywords.length}`
      }}</span>
      <span class="attendance-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="attendance-body">
      <div v-if="failed" class="attendance-notice">
        部分记录暂时无法读取，当前数量可能不完整。<el-button
          link
          @click="
            attendance.refetch();
            feed.refetch();
          "
          >重试</el-button
        >
      </div>
      <p class="attendance-note">
        {{
          enabled
            ? "直播自动签到已启用。"
            : "本课程直播自动签到未启用或已暂停。"
        }}
        二维码按签到记录计数，同一次签到不重复累计；关键词为识别线索，签到结果以接口记录为准。
      </p>
      <p v-if="loading && !total" class="attendance-note">
        正在读取本节课记录…
      </p>
      <p v-else-if="!total && !failed" class="attendance-empty">
        本节课暂无签到码记录或点名关键词，发现后会自动更新。
      </p>
      <div v-if="codes.length" class="attendance-section">
        <h3>
          签到二维码 <small>{{ codes.length }}</small>
        </h3>
        <article
          v-for="record in codes"
          :key="record.id"
          class="attendance-record"
        >
          <a
            v-if="record.image"
            :href="fileUrl(record.image)"
            target="_blank"
            rel="noopener noreferrer"
            class="attendance-image"
            aria-label="查看签到二维码原始画面"
            ><img
              :src="fileUrl(record.image)"
              alt="签到二维码课堂画面"
              loading="lazy"
            /><span>查看原图 ↗</span></a
          >
          <div class="attendance-record-content">
            <div class="attendance-record-head">
              <time>{{ date(record.at) }}</time
              ><el-tag
                :type="
                  record.status === 'succeeded'
                    ? 'success'
                    : record.status === 'submitting'
                      ? 'primary'
                      : 'warning'
                "
                >{{ names[record.status] || record.status }}</el-tag
              >
            </div>
            <p>{{ record.message || "暂无更多签到详情" }}</p>
            <small v-if="record.history">签到编号 {{ record.history }}</small>
          </div>
        </article>
      </div>
      <div v-if="keywords.length" class="attendance-section">
        <h3>
          点名 / 签到关键词 <small>{{ keywords.length }}</small>
        </h3>
        <article
          v-for="event in keywords"
          :key="event.id"
          class="attendance-record"
        >
          <div class="attendance-record-content">
            <div class="attendance-record-head">
              <time>{{ timecode(event.start) }} · 课堂内时间</time
              ><el-tag type="warning">{{ event.keyword }}</el-tag>
            </div>
            <blockquote>{{ event.text }}</blockquote>
          </div>
        </article>
      </div>
    </div>
  </details>
</template>
<style scoped>
.lesson-attendance {
  margin-top: 12px;
  border: 1px solid var(--ochrelune-border);
  border-radius: var(--ochrelune-radius-panel);
  background: var(--ochrelune-surface-subtle);
  overflow: hidden;
}
summary {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  cursor: pointer;
  list-style: none;
  color: var(--ochrelune-text);
}
summary::-webkit-details-marker {
  display: none;
}
summary:focus-visible {
  outline: 2px solid var(--ochrelune-focus);
  outline-offset: -3px;
}
.attendance-count {
  min-width: 24px;
  padding: 2px 7px;
  border-radius: 20px;
  background: var(--ochrelune-surface-strong);
  text-align: center;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.attendance-summary {
  color: var(--ochrelune-text-secondary);
  font-size: 12px;
}
.attendance-chevron {
  margin-left: auto;
  transition: transform 0.15s;
}
details[open] .attendance-chevron {
  transform: rotate(180deg);
}
.attendance-body {
  border-top: 1px solid var(--ochrelune-border);
  padding: 0 16px 16px;
  max-height: 520px;
  overflow-y: auto;
}
.attendance-note,
.attendance-empty {
  color: var(--ochrelune-text-secondary);
  font-size: 12px;
  line-height: 1.8;
}
.attendance-empty {
  padding: 16px 0;
  text-align: center;
}
.attendance-notice {
  color: var(--ochrelune-danger);
  font-size: 13px;
  padding-top: 12px;
}
h3 {
  font-size: 14px;
  margin: 18px 0 10px;
}
h3 small {
  color: var(--ochrelune-text-secondary);
  margin-left: 6px;
}
.attendance-record {
  display: flex;
  gap: 16px;
  padding: 14px;
  background: var(--ochrelune-surface);
  border: 1px solid var(--ochrelune-border);
  border-radius: 8px;
  margin-top: 8px;
}
.attendance-image {
  width: 160px;
  flex-shrink: 0;
  font-size: 12px;
}
.attendance-image img {
  display: block;
  width: 100%;
  border-radius: 5px;
  margin-bottom: 6px;
}
.attendance-record-content {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.attendance-record-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
time,
.attendance-record-content small {
  font-size: 12px;
  color: var(--ochrelune-text-secondary);
}
.attendance-record p,
blockquote {
  margin: 10px 0 0;
  line-height: 1.8;
  font-size: 14px;
}
@media (max-width: 600px) {
  summary {
    flex-wrap: wrap;
  }
  .attendance-summary {
    order: 5;
    flex-basis: 100%;
  }
  .attendance-record {
    flex-direction: column;
  }
  .attendance-image {
    width: 100%;
  }
}
</style>
