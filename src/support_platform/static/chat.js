/**
 * Phase4 chat client (S25).
 * Renders public_steps, suggestion, knowledge_basis, claim provenance (LOCAL|WEB).
 * Never reads or displays cot / prompt / api_key / internal fields.
 */
(function () {
  "use strict";

  var conversationId = null;
  var NON_LOCAL = [
    "非基于本地知识",
    "不是基于本地知识",
    "非本地知识库",
    "不基于本地知识库",
    "来自网络",
    "网络来源",
  ];

  function $(id) {
    return document.getElementById(id);
  }

  function appendMessage(role, text) {
    var box = $("messages");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "msg " + role;
    el.textContent = text || "";
    box.appendChild(el);
  }

  function stepDetailLines(step) {
    var lines = [];
    var kind = String(step.kind || "");
    if (step.question_preview) {
      lines.push("理解问题：" + String(step.question_preview));
    }
    if (step.outline) {
      lines.push("计划：" + String(step.outline));
    }
    if (step.query) {
      lines.push("检索词：" + String(step.query));
    }
    if (typeof step.hit_count === "number") {
      lines.push("本地命中 " + step.hit_count + " 条");
    }
    if (Array.isArray(step.chunk_ids) && step.chunk_ids.length) {
      lines.push("证据片段：" + step.chunk_ids.slice(0, 5).join("、"));
    }
    if (Array.isArray(step.sources) && step.sources.length) {
      step.sources.slice(0, 5).forEach(function (src, idx) {
        if (!src || typeof src !== "object") return;
        var title = src.title || src.url || "来源 " + (idx + 1);
        var bit = "网络来源 " + (idx + 1) + "：" + title;
        if (src.url) bit += " — " + src.url;
        lines.push(bit);
      });
    }
    if (step.knowledge_basis) {
      lines.push("来源判定：" + String(step.knowledge_basis));
    }
    if (step.result_status) {
      lines.push("结果状态：" + String(step.result_status));
    }
    if (step.tool && kind === "tool_rejected") {
      lines.push("拒绝工具：" + String(step.tool));
    }
    return lines;
  }

  function renderSteps(steps) {
    var list = $("steps-list");
    if (!list) return;
    list.innerHTML = "";
    (steps || []).forEach(function (step, index) {
      if (!step || typeof step !== "object") return;
      var li = document.createElement("li");
      li.className = "step-item kind-" + String(step.kind || "step");
      var title = document.createElement("div");
      title.className = "step-title";
      title.textContent =
        String(index + 1) + ". " + (step.label || step.kind || "步骤");
      li.appendChild(title);
      var details = stepDetailLines(step);
      if (details.length) {
        var ul = document.createElement("ul");
        ul.className = "step-details";
        details.forEach(function (line) {
          var d = document.createElement("li");
          d.textContent = line;
          ul.appendChild(d);
        });
        li.appendChild(ul);
      }
      list.appendChild(li);
    });
  }

  function renderSuggestion(payload) {
    var output = (payload && payload.output) || payload || {};
    var summary = output.summary || payload.summary || "";
    var basis = output.knowledge_basis || payload.knowledge_basis || "—";
    var claims = output.claims || payload.claims || [];

    var summaryEl = $("summary");
    if (summaryEl) summaryEl.textContent = summary;

    var basisWrap = $("knowledge-basis");
    var basisVal = $("basis-value");
    if (basisVal) basisVal.textContent = basis;
    if (basisWrap) basisWrap.setAttribute("data-active", String(basis));

    var disclosure = $("disclosure");
    if (disclosure) {
      var need =
        basis === "WEB" ||
        basis === "MIXED" ||
        NON_LOCAL.some(function (m) {
          return String(summary).indexOf(m) >= 0;
        });
      disclosure.hidden = !need;
    }

    var claimsEl = $("claims");
    if (claimsEl) {
      claimsEl.innerHTML = "";
      (claims || []).forEach(function (claim) {
        if (!claim || typeof claim !== "object") return;
        var li = document.createElement("li");
        var badge = document.createElement("span");
        var prov = String(claim.provenance || "LOCAL").toUpperCase();
        if (prov !== "LOCAL" && prov !== "WEB") prov = "LOCAL";
        badge.className = "prov " + prov;
        badge.textContent = prov;
        var text = document.createElement("span");
        text.textContent = claim.text || "";
        li.appendChild(badge);
        li.appendChild(text);
        claimsEl.appendChild(li);
      });
    }
  }

  function ensureConversation() {
    if (conversationId) return Promise.resolve(conversationId);
    return fetch("/api/v2/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ title: "chat" }),
    }).then(function (res) {
      if (!res.ok) throw new Error("create conversation failed");
      return res.json();
    }).then(function (body) {
      conversationId = body.conversation_id || body.id;
      return conversationId;
    });
  }

  function formatErrorDetail(body, fallback) {
    if (!body) return fallback;
    var d = body.detail;
    if (typeof d === "string" && d.trim()) return d;
    if (Array.isArray(d) && d.length) {
      return d
        .map(function (x) {
          return (x && (x.msg || x.message)) || String(x);
        })
        .join("; ");
    }
    return fallback;
  }

  function refreshRuntimeBanner() {
    var banner = $("runtime-banner");
    if (!banner) return;
    fetch("/health/ready", { credentials: "same-origin" })
      .then(function (res) {
        if (res.ok) {
          banner.hidden = true;
          banner.textContent = "";
          return;
        }
        banner.hidden = false;
        banner.textContent =
          "依赖未就绪（数据库/Redis）。请先启动 Docker Desktop，再执行 docker compose up -d db redis。当前发消息会失败。";
      })
      .catch(function () {
        banner.hidden = false;
        banner.textContent = "无法连接后端健康检查。请确认 uvicorn 仍在运行（:8800）。";
      });
  }

  function sendMessage(content) {
    return ensureConversation().then(function (id) {
      var key =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : String(Date.now());
      var controller =
        typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = null;
      if (controller) {
        timer = setTimeout(function () {
          controller.abort();
        }, 30000);
      }
      return fetch("/api/v2/conversations/" + encodeURIComponent(id) + "/messages", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        credentials: "same-origin",
        body: JSON.stringify({ content: content }),
        signal: controller ? controller.signal : undefined,
      })
        .then(function (res) {
          if (!res.ok) {
            return res.json().catch(function () { return {}; }).then(function (body) {
              throw new Error(
                formatErrorDetail(body, "HTTP " + res.status + " 发送失败")
              );
            });
          }
          return res.json().catch(function () {
            return {};
          });
        })
        .then(function () {
          return fetch("/api/v2/conversations/" + encodeURIComponent(id), {
            credentials: "same-origin",
            signal: controller ? controller.signal : undefined,
          });
        })
        .then(function (res) {
          if (!res.ok) throw new Error("读取对话详情失败 HTTP " + res.status);
          return res.json();
        })
        .finally(function () {
          if (timer) clearTimeout(timer);
        });
    });
  }

  function setBusy(busy) {
    var btn = $("send-btn");
    var input = $("chat-input");
    if (btn) {
      btn.disabled = !!busy;
      btn.textContent = busy ? "处理中…" : "发送";
    }
    if (input) input.disabled = !!busy;
  }

  function login(username, password) {
    return fetch("/api/v2/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username: username, password: password }),
    }).then(function (res) {
      var err = $("login-error");
      if (!res.ok) {
        if (err) {
          err.hidden = false;
          err.textContent = "登录失败";
        }
        return;
      }
      if (err) err.hidden = true;
      var bar = $("session-bar");
      var label = $("session-label");
      if (bar) bar.hidden = false;
      if (label) label.textContent = username;
    });
  }

  function logout() {
    return fetch("/api/v2/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    }).then(function () {
      var bar = $("session-bar");
      if (bar) bar.hidden = true;
    });
  }

  function init() {
    refreshRuntimeBanner();
    setInterval(refreshRuntimeBanner, 15000);
    var form = $("chat-form");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var input = $("chat-input");
        var text = (input && input.value) || "";
        text = text.trim();
        if (!text) return;
        appendMessage("user", text);
        if (input) input.value = "";
        setBusy(true);
        appendMessage("assistant", "正在检索本地知识与网络来源，请稍候…");
        var pending = $("messages") && $("messages").lastElementChild;
        sendMessage(text)
          .then(function (detail) {
            var summary =
              (detail.output && detail.output.summary) ||
              detail.summary ||
              "";
            if (pending && pending.parentNode) pending.parentNode.removeChild(pending);
            appendMessage("assistant", summary || "(无建议正文)");
            renderSteps(detail.public_steps || []);
            renderSuggestion(detail);
          })
          .catch(function (err) {
            if (pending && pending.parentNode) pending.parentNode.removeChild(pending);
            var msg =
              (err && err.name === "AbortError")
                ? "请求超时：请确认 Docker Desktop 已启动且数据库可用。"
                : "发送失败：" + ((err && err.message) || "请重试。");
            appendMessage("assistant", msg);
          })
          .then(function () {
            setBusy(false);
          });
      });
    }

    var loginForm = $("login-form");
    if (loginForm) {
      loginForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var u = ($("username") && $("username").value) || "";
        var p = ($("password") && $("password").value) || "";
        login(u.trim(), p);
      });
    }
    var logoutBtn = $("logout-btn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", function () {
        logout();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
