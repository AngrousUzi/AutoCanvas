<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import Hls from "hls.js";
const props = defineProps<{ url?: string; label: string }>();
const video = ref<HTMLVideoElement>();
const status = ref("等待视频源");
let hls: Hls | undefined;
function cleanup() {
  hls?.destroy();
  hls = undefined;
  if (video.value) {
    video.value.pause();
    video.value.removeAttribute("src");
    video.value.load();
  }
}
function start() {
  cleanup();
  const media = video.value;
  if (!media || !props.url) {
    status.value = "等待视频源";
    return;
  }
  media.muted = true;
  status.value = "正在连接";
  if (media.canPlayType("application/vnd.apple.mpegurl")) {
    media.src = props.url;
  } else if (Hls.isSupported()) {
    hls = new Hls();
    hls.loadSource(props.url);
    hls.attachMedia(media);
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        status.value = "连接失败，请重试或刷新下方地址";
        hls?.destroy();
        hls = undefined;
      }
    });
  } else {
    status.value = "浏览器不支持此视频流，请使用下方地址";
    return;
  }
  void media.play().catch(() => {
    status.value = "点击播放开始观看";
  });
}
watch([video, () => props.url], start, { flush: "post" });
onBeforeUnmount(cleanup);
</script>
<template>
  <section class="camera">
    <div class="camera-heading">
      <b>{{ label }}</b
      ><span>{{ status }}</span
      ><button v-if="url" @click="start">重试</button>
    </div>
    <video
      ref="video"
      controls
      muted
      autoplay
      playsinline
      :aria-label="label"
      @playing="status = '播放中'"
      @waiting="status = '缓冲中'"
      @pause="status = '已暂停'"
      @error="status = '播放失败，请刷新视频流地址'"
    />
    <div v-if="!url" class="camera-placeholder">暂无镜头信号</div>
  </section>
</template>
<style scoped>
.camera {
  position: relative;
  overflow: hidden;
  border-radius: var(--ochrelune-radius-panel);
  background: var(--ochrelune-action);
  color: var(--ochrelune-on-action);
}
.camera-heading {
  height: 40px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
}
.camera-heading span {
  margin-left: auto;
  color: var(--ochrelune-on-dark-muted);
}
.camera-heading button {
  background: transparent;
  color: var(--ochrelune-on-action);
  border: 0;
  cursor: pointer;
}
video {
  display: block;
  width: 100%;
  aspect-ratio: 16/9;
  background: var(--ochrelune-video);
}
.camera-placeholder {
  position: absolute;
  inset: 40px 0 0;
  display: grid;
  place-items: center;
  color: var(--ochrelune-on-dark-muted);
  pointer-events: none;
}
</style>
