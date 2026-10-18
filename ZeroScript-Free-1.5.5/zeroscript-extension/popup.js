// SPDX-License-Identifier: GPL-3.0-or-later
const KOFI_URL = "https://ko-fi.com/sebattfg";
const SUPPORTED_HOSTS = [
  "chat.deepseek.com", "deepseek.com", "chatgpt.com", "chat.openai.com",
  "gemini.google.com", "www.kimi.ai", "kimi.ai",
  "chat.z.ai", "chat.qwen.ai", "arena.ai", "www.meta.ai", "meta.ai",
];
const DEFAULT_AI_URL = "https://chat.deepseek.com/";

document.getElementById("ver").textContent = `v${chrome.runtime.getManifest().version}`;

function render(s) {
  const dot = document.getElementById("dot");
  const state = document.getElementById("state");
  const tools = document.getElementById("tools");
  const servers = document.getElementById("servers");
  const list = s.servers || [];
  // The Dev Kit (id "zeroscript", flag local) runs inside the Bridge and is
  // always alive - counting it here would light the dot up with Roblox Studio
  // closed. Connectivity tracks real MCP servers only, preferring the primary
  // one exactly like the in-page bar does.
  const real = list.filter((x) => !x.local && x.id !== "zeroscript");
  const roblox = real.find((x) => x.id === "roblox");
  const up = real.filter((x) => x.alive).length;
  const mcpOk = s.connected && (roblox
    ? (!!roblox.alive || (roblox.tools || 0) > 0)
    : (s.mcpAlive || up > 0 || s.tools > 0));
  const studioOff = mcpOk && s.studio === false; // MCP up but no Studio attached
  const ok = mcpOk && !studioOff;
  dot.className = "dot " + (s.connected ? (ok ? "on" : "warn") : "");
  state.textContent = s.connected
    ? (ok ? "Connected · Roblox Studio ready"
        : studioOff ? "Studio not connected · enable the MCP server in Studio"
        : "Bridge OK · open Roblox Studio")
    : "Bridge offline";
  const total = (s.tools || 0) + (s.localTools || 0);
  tools.textContent = s.connected ? `${total} tools available` : "Run bridge.py";
  const label = (x) => (x.id === "zeroscript" ? "zeroscript dev kit (built in)" : x.id);
  servers.textContent = s.connected
    ? list.map((x) => `${x.alive ? "●" : "○"} ${label(x)} (${x.alive ? x.tools + " tools" : "down"})`).join("\n")
    : "";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "status" }, (s) => s && render(s));
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 600));
});
document.getElementById("restart").addEventListener("click", (e) => {
  e.target.textContent = "Restarting…";
  chrome.runtime.sendMessage({ type: "restart_mcp" }, () => {
    e.target.textContent = "⟳ Restart Roblox server";
    setTimeout(refresh, 600);
  });
});
document.getElementById("kofi").addEventListener("click", () => {
  chrome.tabs.create({ url: KOFI_URL });
});
document.getElementById("settings").addEventListener("click", () => {
  // Same mechanism as the Ko-fi button (chrome.tabs), but tries the in-page
  // panel on an already-open supported AI tab first, so opening it doesn't
  // require a conversation to already be started there.
  chrome.tabs.query({}, (tabs) => {
    const active = tabs.find((t) => t.active && t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    const anySupported = active || tabs.find((t) => t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    if (anySupported) {
      chrome.tabs.sendMessage(anySupported.id, { type: "zs-open-menu" });
      chrome.tabs.update(anySupported.id, { active: true });
    } else {
      chrome.tabs.create({ url: DEFAULT_AI_URL });
    }
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "zs-status") render(msg);
});
refresh();
setInterval(refresh, 2000);
