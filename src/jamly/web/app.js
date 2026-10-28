(function () {
  "use strict";
  const byId = id => document.getElementById(id);
  const config = { url: location.origin, token: sessionStorage.getItem("jamly-token") || "" };
  let models = [];
  let initialized = false;
  let stopped = false;
  let refreshing = false;
  let timer;
  let connectionError = "";
  const stages = {
    pending: "Queued", metadata: "Checking media", download: "Downloading audio",
    separate: "Loading model / separating audio (first use downloads weights)",
    mix: "Preparing exports", publish: "Saving files", done: "Complete",
    skipped: "Reused existing files", error: "Failed", cancelled: "Cancelled", running: "Working",
  };

  function showError(error) {
    byId("error").textContent = error.message || String(error);
  }

  async function action(button, task) {
    button.disabled = true;
    byId("error").textContent = "";
    try {
      await task();
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  }

  const post = (path, body = {}) => jamlyClient.request(config, path, "POST", body);

  function modelHelp() {
    const description = models.find(model => model.id === byId("model").value)?.description || "";
    byId("model-help").textContent = description;
    byId("model").title = description;
  }

  function settings() {
    return {
      model: byId("model").value, audio_format: byId("format").value,
      output_dir: byId("output-dir").value.trim(),
    };
  }

  function button(label, task) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.addEventListener("click", () => action(element, task));
    return element;
  }

  function download(output) {
    const url = new URL(jamlyClient.outputURL(config, output.url));
    const form = document.createElement("form");
    form.method = "post";
    form.target = "_blank";
    form.rel = "noopener";
    form.action = "/app/download/" + url.pathname.slice("/outputs/".length) + url.search;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "token";
    input.value = config.token;
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
    form.remove();
  }

  function render(status) {
    models = status.models;
    if (!initialized) {
      byId("model").replaceChildren();
      for (const model of models) {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = model.label;
        byId("model").appendChild(option);
      }
      byId("model").value = status.defaults.model;
      byId("format").value = status.defaults.audio_format;
      byId("output-dir").value = status.defaults.output_dir;
      modelHelp();
      initialized = true;
    }
    byId("workspace").hidden = false;
    byId("pairing").hidden = true;
    byId("counts").textContent =
      `Queued: ${status.pending} | Complete: ${status.done} | Failed: ${status.failed}`;
    byId("jobs").replaceChildren();
    for (const job of status.jobs) {
      const row = document.createElement("li");
      const title = document.createElement("p");
      title.className = "job-title";
      title.textContent = job.title || job.watch_id;
      row.appendChild(title);
      const detail = document.createElement("p");
      const stage = status.current?.watch_id === job.watch_id ? status.current.stage : job.status;
      const model = models.find(choice => choice.id === job.model)?.label || job.model;
      detail.textContent = `${stages[stage] || stage} | ${model} | ${job.audio_format.toUpperCase()}`;
      row.appendChild(detail);
      if (job.error) {
        const error = document.createElement("p");
        error.className = "error";
        error.textContent = job.error;
        row.appendChild(error);
      }
      if (job.outputs.length) {
        row.appendChild(button("Show files", () => post("/app/open-folder", { id: job.watch_id })));
        for (const output of job.outputs) {
          if (output.kind === "metadata") continue;
          row.appendChild(button(
            "Save a copy: " + (output.kind === "no_drums" ? "drumless" : "drums"),
            () => download(output),
          ));
        }
      }
      if (!["pending", "running"].includes(job.status)) {
        row.appendChild(button("Retry / check files", async () => {
          await jamlyClient.retry(config, job.watch_id, byId("replace").checked);
          await refresh();
        }));
      }
      byId("jobs").appendChild(row);
    }
    if (status.error) showError(new Error(status.error));
  }

  async function refresh() {
    if (stopped || refreshing || document.hidden) return;
    clearTimeout(timer);
    if (!config.token) {
      byId("connection").textContent = "Open the Jamly application to connect this page.";
      byId("pairing").hidden = false;
      return;
    }
    refreshing = true;
    try {
      const status = await jamlyClient.status(config);
      byId("connection").textContent = "Connected to your local companion. Jobs continue when this page closes.";
      if (byId("error").textContent === connectionError) byId("error").textContent = "";
      connectionError = "";
      render(status);
    } catch (error) {
      byId("connection").textContent = "Offline or session expired. Reopen Jamly; displayed jobs may be stale.";
      byId("pairing").hidden = false;
      connectionError = error.message || String(error);
      showError(error);
    } finally {
      refreshing = false;
      if (!stopped) timer = setTimeout(refresh, 2000);
    }
  }

  byId("model").addEventListener("change", modelHelp);
  byId("pair-form").addEventListener("submit", async event => {
    event.preventDefault();
    await action(event.submitter, async () => {
      config.token = byId("pair-token").value.trim();
      await jamlyClient.status(config);
      sessionStorage.setItem("jamly-token", config.token);
      byId("pair-token").value = "";
      await refresh();
    });
  });
  byId("save-settings").addEventListener("click", () => action(byId("save-settings"), async () => {
    await post("/settings", settings());
    byId("connection").textContent = "Defaults saved. Already queued jobs keep their settings.";
  }));
  byId("split-form").addEventListener("submit", async event => {
    event.preventDefault();
    await action(byId("submit"), async () => {
      const inputs = byId("inputs").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
      if (!inputs.length) throw new Error("Enter at least one individual media URL.");
      const options = settings();
      await post("/settings", options);
      const result = await jamlyClient.addInputs(config, inputs, byId("replace").checked, options);
      byId("inputs").value = "";
      await refresh();
      byId("connection").textContent = `Queued ${result.newly_added} new job(s).`;
    });
  });
  byId("open-output").addEventListener("click", () => action(byId("open-output"), async () => {
    await post("/settings", settings());
    await post("/app/open-folder");
  }));
  byId("prepare-extension").addEventListener("click", () => action(byId("prepare-extension"), async () => {
    const extension = await post("/app/extension");
    byId("extension-path").textContent = extension.directory;
    byId("extension-url").textContent = location.origin;
    byId("extension-token").value = extension.token;
    byId("extension-setup").hidden = false;
  }));
  byId("copy-token").addEventListener("click", () => action(byId("copy-token"), async () => {
    await navigator.clipboard.writeText(byId("extension-token").value);
    byId("connection").textContent = "Pairing token copied. Keep it private.";
  }));
  byId("quit").addEventListener("click", () => action(byId("quit"), async () => {
    if (!window.confirm("Stop the companion? Unfinished jobs will resume the next time you open Jamly.")) return;
    await post("/app/stop");
    stopped = true;
    clearTimeout(timer);
    sessionStorage.removeItem("jamly-token");
    byId("workspace").hidden = true;
    byId("connection").textContent = "Companion stopped. Open Jamly again to resume.";
  }));
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden) refresh();
  });
  window.addEventListener("unload", () => { stopped = true; clearTimeout(timer); });
  refresh();
})();
