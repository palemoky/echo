// Charts are declared as <div class="chart" data-chart='{...}'> so no inline scripts are needed.
(function () {
  const dark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;

  function initCharts(root) {
    if (!window.echarts) return;
    root.querySelectorAll("[data-chart]").forEach((el) => {
      if (el.dataset.ready) return;
      el.dataset.ready = "1";
      const option = JSON.parse(el.dataset.chart);
      option.backgroundColor = "transparent";
      if (option.tooltip && option.tooltip.valueFormatter === "percent") {
        option.tooltip.valueFormatter = (v) => v + "%";
      }
      const chart = echarts.init(el, dark() ? "dark" : null);
      chart.setOption(option);
      const video = el.dataset.videoUrl;
      if (video) {
        chart.on("click", (params) => {
          const [m, s] = String(params.name).split(":").map(Number);
          if (!Number.isNaN(m)) window.open(`${video}?t=${m * 60 + (s || 0)}`, "_blank", "noopener");
        });
      }
      new ResizeObserver(() => chart.resize()).observe(el);
    });
  }

  htmx.onLoad(initCharts);

  // Let the server's error partials replace the flash area even on 4xx/5xx.
  document.body.addEventListener("htmx:beforeSwap", (evt) => {
    if (evt.detail.xhr.status >= 400 && evt.detail.xhr.status < 600) {
      evt.detail.shouldSwap = true;
      evt.detail.target = document.getElementById("flash");
    }
  });
  document.body.addEventListener("htmx:sendError", () => {
    document.getElementById("flash").innerHTML =
      '<div class="flash error">无法连接到 echo 服务，请确认 echo-voc web 仍在运行。</div>';
  });
})();
