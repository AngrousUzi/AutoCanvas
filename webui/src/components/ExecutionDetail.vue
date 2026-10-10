<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { useRouter } from "vue-router";
import { api } from "../api";
import { useAction } from "../queries";
import { useWorkspace } from "../useWorkspace";
import { date, kinds, statuses, tone } from "../format";
import {
  errorAdvice,
  findLecture,
  isActive,
  lectureTitle,
  timecode,
} from "../workspace";
const props = defineProps<{ id: string | null }>();
const emit = defineEmits<{ close: [] }>();
const { runs, lectures, courseName } = useWorkspace();
const router = useRouter();
const { action, busy } = useAction();
const run = computed(() => runs.value.find((r) => r.id === props.id));
const progress = useQuery({
  queryKey: computed(() => ["progress", props.id]),
  queryFn: () => api("/api/executions/" + props.id + "/progress"),
  enabled: computed(() => !!props.id),
  refetchInterval: computed(() =>
    run.value && isActive(run.value) ? 3000 : false,
  ),
});
const eventNames: Record<string, string> = {
  monitor_start: "开始监听",
  monitor_stop: "结束监听",
  connected: "连接到直播",
  connection_interrupted: "连接中断，尝试恢复",
  authentication_required: "等待认证恢复",
  audio_gap: "音频出现缺口",
  keyword: "关键词命中",
};
function openOutputs() {
  if (!run.value) return;
  router.push({
    path: "/files",
    query: {
      course: run.value.course_id,
      lecture: run.value.lecture_id || undefined,
      section: run.value.kind === "assignments" ? "assignments" : undefined,
      sample: run.value.kind.startsWith("sample_") ? "1" : undefined,
    },
  });
  emit("close");
}
</script>
<template>
  <el-dialog
    :model-value="!!id"
    title="执行详情"
    width="min(740px,94vw)"
    @close="emit('close')"
  >
    <template v-if="run">
      <div class="section-head">
        <div>
          <p class="muted">{{ courseName(run.course_id) }}</p>
          <h3>
            {{
              run.lecture_id
                ? lectureTitle(findLecture(run, lectures))
                : kinds[run.kind]
            }}
          </h3>
        </div>
        <el-tag :type="tone(run.status)">{{ statuses[run.status] }}</el-tag>
      </div>
      <div class="detail-facts">
        <div>
          <span>处理内容</span><b>{{ kinds[run.kind] }}</b>
        </div>
        <div>
          <span>最近更新</span><b>{{ date(run.updated) }}</b>
        </div>
        <div>
          <span>执行次数</span><b>{{ run.attempts }} 次</b>
        </div>
        <div>
          <span>发起方式</span
          ><b>{{ run.options?.automatic ? "自动安排" : "手动发起" }}</b>
        </div>
      </div>
      <el-alert
        v-if="run.error"
        :title="errorAdvice(run.error)"
        type="warning"
        :closable="false"
      />
      <div v-if="progress.data.value" class="progress-summary">
        <span v-if="run.kind.includes('asr') || run.kind === 'live'"
          >转写进度
          <b>{{ timecode(progress.data.value.processed_seconds) }}</b></span
        ><span v-if="progress.data.value.slides !== undefined"
          >已整理 <b>{{ progress.data.value.slides }} 张画面</b></span
        >
      </div>
      <p v-if="progress.isPending.value" class="muted">正在读取处理进度…</p>
      <p v-if="progress.isError.value" class="muted">
        暂时无法读取进度，请稍后重试。
      </p>
      <div v-if="progress.data.value?.events?.length" class="event-list">
        <h4>最近活动</h4>
        <div
          v-for="(event, i) in [...progress.data.value.events]
            .reverse()
            .slice(0, 12)"
          :key="i"
          class="event-item"
        >
          <span class="event-dot"></span>
          <div>
            <b>{{ eventNames[event.type] || "处理事件" }}</b>
            <p v-if="event.text">{{ event.text }}</p>
            <small
              >{{ event.at ? date(event.at) : timecode(event.start)
              }}{{ event.keyword ? " · " + event.keyword : "" }}</small
            >
          </div>
        </div>
      </div>
      <div class="dialog-actions">
        <el-button v-if="run.artifact" type="primary" @click="openOutputs"
          >打开产物</el-button
        ><el-button
          v-if="['failed', 'cancelled', 'needs_login'].includes(run.status)"
          :loading="busy"
          @click="action('/api/executions/' + run.id + '/retry')"
          >重新执行</el-button
        ><el-button
          v-if="isActive(run)"
          :loading="busy"
          type="danger"
          plain
          @click="action('/api/executions/' + run.id + '/cancel')"
          >取消任务</el-button
        ><RouterLink
          v-if="run.status === 'needs_login'"
          to="/auth"
          @click="emit('close')"
          >检查登录 →</RouterLink
        >
      </div>
      <details>
        <summary>诊断信息</summary>
        <dl class="readable-fields">
          <dt>执行编号</dt>
          <dd>{{ run.id }}</dd>
          <dt v-if="run.error">错误类型</dt>
          <dd v-if="run.error">{{ run.error }}</dd>
        </dl>
        <RouterLink to="/logs" @click="emit('close')"
          >查看服务日志 →</RouterLink
        >
      </details> </template
    ><el-empty v-else description="这条执行记录暂不可用" />
  </el-dialog>
</template>
