// 기본값. UI 설정에서 입력하면 localStorage 값이 우선합니다.
window.APP_CONFIG = {
  // ngrok static domain — 고정 URL이라 watchdog가 갱신하지 않습니다.
  // tunnel.json도 같은 값으로 커밋돼 있고, 이 값은 그 fallback입니다.
  defaultApiUrl: "https://refurbish-anew-purveyor.ngrok-free.dev",
  defaultTopK: 5,
};
