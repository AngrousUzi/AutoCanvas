<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { useApiQuery as query } from "../queries";
import { useWorkspace } from "../useWorkspace";
import { api, fileUrl } from "../api";
import { date } from "../format";
import { timecode } from "../workspace";
interface ClassroomEvent {
  id: string;
  type: "qr" | "keyword";
  source: string;
  course_id: string;
  lecture_id: string;
  start: number;
  content?: string;
  decoded?: boolean;
  image?: string;
  keyword?: string;
  text?: string;
}
interface Scan {
  course_id: string;
  lecture_id: string;
  status: string;
  scanned_images: number;
  total_images: number;
  count: number;
}
const attendance = query("attendance", "/api/attendance", 1500);
const savingAttendance = ref(false);
const attendanceScope = ref<string[]>([]);
watch(
  () => attendance.data.value?.config?.course_ids,
  (value) => {
    if (value) attendanceScope.value = [...value];
  },
  { immediate: true },
);
const attendanceNames: Record<string, string> = {
  submitting: "正在签到",
  succeeded: "接口确认成功",
  expired: "码已失效，等待新码",
  needs_login: "等待重新认证",
  needs_action: "需要现场操作",
  rejected: "学校拒绝",
  unknown: "结果不确定，请核对",
};
const attendanceAuth: Record<string, string> = {
  idle: "等待直播二维码",
  authenticating: "正在认证",
  ready: "登录已就绪",
  needs_login: "需要登录",
  unavailable: "认证服务暂不可用",
};
async function saveAttendance(enabled: boolean) {
  savingAttendance.value = true;
  try {
    await api("/api/attendance", "PATCH", {
      enabled,
      course_ids: attendanceScope.value,
    });
    await attendance.refetch();
    ElMessage.success(
      enabled
        ? "自动签到已启用，只处理直播最新画面"
        : "自动签到已关闭，已发出的请求仍可能完成",
    );
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    savingAttendance.value = false;
  }
}
const route = useRoute();
const feed = query("classroom-events", "/api/classroom-events", 10000);
const { courses, lectures, courseName } = useWorkspace();
const course = ref(String(route.query.course || "")),
  type = ref("all"),
  search = ref(""),
  page = ref(1),
  preview = ref<ClassroomEvent | null>(null);
const events = computed<ClassroomEvent[]>(() => feed.data.value?.events || []);
const scans = computed<Scan[]>(() => feed.data.value?.scans || []);
function lecture(event: ClassroomEvent) {
  return lectures.value.find(
    (l) =>
      l.course_id === event.course_id &&
      l.id === event.lecture_id &&
      l.kind === event.source,
  );
}
function eventTime(event: ClassroomEvent) {
  const begin = Date.parse(lecture(event)?.begin || "");
  return Number.isFinite(begin) ? begin + event.start * 1000 : 0;
}
const scoped = computed(() =>
  events.value.filter((e) => !course.value || e.course_id === course.value),
);
const filtered = computed(() =>
  scoped.value
    .filter(
      (e) =>
        (type.value === "all" || e.type === type.value) &&
        `${courseName(e.course_id)} ${e.keyword || ""} ${e.text || ""} ${e.content || ""}`
          .toLowerCase()
          .includes(search.value.toLowerCase()),
    )
    .sort(
      (a, b) =>
        eventTime(b) - eventTime(a) ||
        b.start - a.start ||
        a.id.localeCompare(b.id),
    ),
);
const scanScope = computed(() =>
  scans.value.filter((s) => !course.value || s.course_id === course.value),
);
const incomplete = computed(
  () => scanScope.value.filter((s) => s.status !== "complete").length,
);
const pageCount = computed(() =>
  Math.max(1, Math.ceil(filtered.value.length / 18)),
);
watch([course, type, search], () => (page.value = 1));
watch(pageCount, (n) => (page.value = Math.min(page.value, n)));
async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success("已复制二维码内容");
  } catch {
    ElMessage.info("当前浏览器无法访问剪贴板，请在详情中选择文字复制。");
  }
}
</script>
<template>
  <section class="panel" style="margin-bottom: 24px">
    <div class="workspace-toolbar">
      <div>
        <h2>直播自动签到</h2>
        <p class="muted">只使用最新直播画面；回放二维码不参与自动签到。</p>
      </div>
      <el-switch
        aria-label="启用直播自动签到"
        :model-value="!!attendance.data.value?.config?.enabled"
        :loading="savingAttendance"
        :disabled="!attendance.data.value"
        @change="(value: string | number | boolean) => saveAttendance(Boolean(value))"
        active-text="已启用"
        inactive-text="已关闭"
      />
    </div>
    <div class="toolbar">
      <el-select
        v-model="attendanceScope"
        multiple
        clearable
        placeholder="全部已启用课程"
        aria-label="自动签到课程范围"
        style="min-width: 300px"
      >
        <el-option
          v-for="c in courses"
          :key="c.id"
          :label="c.name"
          :value="c.id"
        />
      </el-select>
      <el-button
        :loading="savingAttendance"
        @click="saveAttendance(!!attendance.data.value?.config?.enabled)"
        >保存课程范围</el-button
      >
      <span class="muted">{{
        attendanceAuth[attendance.data.value?.auth_status] || "正在连接…"
      }}</span>
    </div>
    <p class="muted">
      每 0.5 秒取帧，旧帧直接替换。认证完成后重选最新解码结果；本地超过 2
      秒的结果不发送。直播源本身的延迟不包含在此时间内。
    </p>
    <el-alert
      v-if="attendance.isError.value"
      title="无法读取自动签到服务状态"
      type="error"
      :closable="false"
    />
    <el-alert
      v-if="attendance.data.value?.automation_paused"
      title="全局自动化已暂停，直播自动签到同步暂停"
      type="warning"
      :closable="false"
    />
    <el-alert
      v-if="attendance.data.value?.error"
      :title="attendance.data.value.error"
      type="warning"
      :closable="false"
    />
    <div v-if="attendance.data.value?.views?.length">
      <h3>正在观察的直播</h3>
      <div
        v-for="v in attendance.data.value.views"
        :key="v.course_id + ':' + v.lecture_id + ':' + v.view"
        class="notice-strip"
      >
        {{ courseName(v.course_id) }} · 画面 {{ v.view || "默认" }} · 最近画面
        {{ date(v.last_frame) }} · 解码 {{ v.decode_ms ?? "—" }} ms · 已替换旧帧
        {{ v.superseded }}
      </div>
    </div>
    <p v-else class="muted">
      {{
        attendance.data.value?.config?.enabled
          ? "当前没有可观察的直播，课程开播后自动连接。"
          : "开启后，在课程直播时检测并处理签到码。"
      }}
    </p>
    <h3>自动签到记录</h3>
    <el-table
      :data="attendance.data.value?.records || []"
      empty-text="还没有自动签到记录"
      max-height="420"
    >
      <el-table-column label="课程" min-width="140"
        ><template #default="{ row }">{{
          courseName(row.course_id)
        }}</template></el-table-column
      >
      <el-table-column label="时间" min-width="170"
        ><template #default="{ row }">{{
          date(row.at)
        }}</template></el-table-column
      >
      <el-table-column label="结果" min-width="155"
        ><template #default="{ row }"
          ><el-tag
            :type="
              row.status === 'succeeded'
                ? 'success'
                : row.status === 'submitting'
                  ? 'primary'
                  : 'warning'
            "
            >{{ attendanceNames[row.status] || row.status }}</el-tag
          ></template
        ></el-table-column
      >
      <el-table-column label="本地耗时" min-width="155"
        ><template #default="{ row }"
          >发送前 {{ row.age_ms }} ms<br />请求
          {{ row.request_ms ?? "—" }} ms</template
        ></el-table-column
      >
      <el-table-column prop="message" label="详情" min-width="240" />
      <el-table-column label="画面" width="90"
        ><template #default="{ row }"
          ><a
            v-if="row.image"
            :href="fileUrl(row.image)"
            target="_blank"
            rel="noopener"
            >查看 ↗</a
          ></template
        ></el-table-column
      >
    </el-table>
    <p class="muted">
      结果不确定时不会自动重发。先尝试扫码签到；接口确认成功后，定位提示仅作提醒。学校未确认成功时请人工核对。关闭后不再派发新请求。
    </p>
  </section>
  <div class="notice-strip">
    <span
      >点名线索来自直播语音关键词；二维码来自回放已保存的
      Slides。下方为检测线索，实际自动签到结果请查看上方记录。</span
    >
  </div>
  <div class="signal-summary">
    <div class="panel">
      <small>点名 / 签到线索</small
      ><strong>{{ scoped.filter((e) => e.type === "keyword").length }}</strong
      ><span>直播转写命中</span>
    </div>
    <div class="panel">
      <small>二维码出现</small
      ><strong>{{ scoped.filter((e) => e.type === "qr").length }}</strong
      ><span>解码确认后，按画面记录</span>
    </div>
    <div class="panel">
      <small>已完成检测的课次</small
      ><strong
        >{{ scanScope.filter((s) => s.status === "complete").length }}
        <em>/ {{ scanScope.length }}</em></strong
      ><span>抽帧完成后自动检测</span>
    </div>
  </div>
  <div class="workspace-toolbar">
    <div class="segmented" aria-label="课堂事件类型">
      <button
        v-for="[value, label] in [
          ['all', '全部线索'],
          ['keyword', '点名与签到'],
          ['qr', '二维码'],
        ]"
        :key="value"
        :class="{ selected: type === value }"
        :aria-pressed="type === value"
        @click="type = value"
      >
        {{ label }}
      </button>
    </div>
    <el-button :loading="feed.isFetching.value" @click="feed.refetch()"
      >刷新记录</el-button
    >
  </div>
  <div class="toolbar">
    <el-input
      v-model="search"
      placeholder="搜索课程、关键词或二维码内容"
      aria-label="搜索课堂线索"
      clearable
    /><el-select
      v-model="course"
      placeholder="全部课程"
      aria-label="筛选线索课程"
      clearable
      style="width: 260px"
      ><el-option
        v-for="c in courses"
        :key="c.id"
        :label="c.name"
        :value="c.id"
    /></el-select>
  </div>
  <el-alert
    v-if="incomplete"
    :title="`${incomplete} 个课次尚未完成二维码检测，暂无记录不代表画面中没有二维码。`"
    type="warning"
    :closable="false"
  />
  <div v-if="feed.isPending.value" class="panel empty-state">
    正在整理课堂线索…
  </div>
  <div v-else-if="feed.isError.value" class="panel empty-state">
    <h3>记录加载失败</h3>
    <el-button @click="feed.refetch()">重新加载</el-button>
  </div>
  <div v-else-if="!filtered.length" class="panel empty-state">
    <div class="empty-symbol">◎</div>
    <h3>
      {{
        search || course || type !== "all"
          ? "没有符合条件的记录"
          : "暂未发现课堂线索"
      }}
    </h3>
    <p>直播命中关键词或回放画面检出二维码后，会自动出现在这里。</p>
  </div>
  <div v-else class="signal-grid">
    <article
      v-for="event in filtered.slice((page - 1) * 18, page * 18)"
      :key="event.id"
      class="panel signal-card"
    >
      <div class="section-head">
        <span class="kind-chip">{{
          event.type === "qr" ? "二维码" : "疑似点名 / 签到"
        }}</span
        ><el-tag
          :type="
            event.type === 'keyword'
              ? 'warning'
              : event.decoded
                ? 'success'
                : 'info'
          "
          >{{
            event.type === "keyword"
              ? event.keyword
              : event.decoded
                ? "已解码"
                : "未确认候选"
          }}</el-tag
        >
      </div>
      <h3>{{ courseName(event.course_id) }}</h3>
      <p class="muted">
        {{
          lecture(event)?.begin
            ? date(lecture(event)?.begin)
            : `课次 ${event.lecture_id}`
        }}
        · {{ event.source === "live" ? "直播" : "回放" }}
      </p>
      <div class="signal-time">
        <time>{{ timecode(event.start) }}</time
        ><small>课堂内时间</small>
      </div>
      <button
        v-if="event.image"
        class="signal-image"
        :aria-label="`查看 ${courseName(event.course_id)} ${timecode(event.start)} 的二维码画面`"
        @click="preview = event"
      >
        <img
          :src="fileUrl(event.image)"
          loading="lazy"
          alt="检出二维码的课堂画面"
        /><span>放大查看 ↗</span>
      </button>
      <blockquote v-if="event.type === 'keyword'">{{ event.text }}</blockquote>
      <p v-else class="signal-payload">
        {{ event.content || "该候选未通过解码确认。" }}
      </p>
      <div class="signal-actions">
        <el-button v-if="event.type === 'qr'" @click="preview = event"
          >查看详情</el-button
        ><el-button v-if="event.content" link @click="copy(event.content)"
          >复制内容</el-button
        ><RouterLink
          :to="{
            path: '/files',
            query: {
              course: event.course_id,
              lecture: event.lecture_id,
              section: event.type === 'qr' ? 'slides' : 'transcript',
            },
          }"
          >打开课次 →</RouterLink
        >
      </div>
    </article>
  </div>
  <div v-if="filtered.length" class="pagination-bar">
    <span
      >{{ filtered.length }} 条记录 · 第 {{ page }} / {{ pageCount }} 页</span
    >
    <div>
      <el-button :disabled="page === 1" @click="page--">上一页</el-button
      ><el-button :disabled="page === pageCount" @click="page++"
        >下一页</el-button
      >
    </div>
  </div>
  <el-dialog
    :model-value="!!preview"
    title="二维码出现记录"
    width="min(1100px,94vw)"
    @close="preview = null"
    ><template v-if="preview"
      ><h3>
        {{ courseName(preview.course_id) }} · {{ timecode(preview.start) }}
      </h3>
      <img
        v-if="preview.image"
        class="large-slide"
        :src="fileUrl(preview.image)"
        alt="二维码原始画面"
      /><label v-if="preview.content" class="signal-content-label"
        >解码内容<el-input
          :model-value="preview.content"
          type="textarea"
          :rows="4"
          readonly
          aria-label="二维码解码内容"
      /></label>
      <p v-else>该候选未通过解码确认。</p>
      <div class="dialog-actions">
        <el-button v-if="preview.content" @click="copy(preview.content)"
          >复制内容</el-button
        ><a v-if="preview.image" :href="fileUrl(preview.image, true)"
          >下载原图 ↗</a
        >
      </div></template
    ></el-dialog
  >
</template>
