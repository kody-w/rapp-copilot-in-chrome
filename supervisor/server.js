#!/usr/bin/env node
/**
 * rapp-chrome-supervisor
 *
 * A resilient proxy in front of the REAL Claude-in-Chrome MCP bridge
 * (`claude --claude-in-chrome-mcp`). This does not replace that engine —
 * it keeps using it exactly as-is, because it's the one that actually
 * works well (accessibility-tree clicking, screenshot handling, its own
 * permission model). What this adds is automatic detection-and-recovery
 * when it hangs or times out mid-session, instead of leaving the tool
 * call (and the calling agent) stuck.
 *
 * Behavior:
 *  - Forwards tools/list and tools/call transparently to the real child
 *    process (so the tool surface always matches whatever the real
 *    bridge currently exposes).
 *  - A tool call that fails with a message indicating the browser
 *    connection is hung/unresponsive (not an explicit permission denial,
 *    which needs a human) gets one same-connection retry, then if still
 *    failing, a full respawn of the underlying `claude --claude-in-chrome-mcp`
 *    child process plus one more retry.
 *  - Respawns are throttled (max 3 within a rolling 2-minute window) to
 *    avoid a crash loop; beyond that, the real error is surfaced with a
 *    note instead of respawning again.
 *  - After any respawn, tab/browser selection state in the underlying
 *    bridge is reset (it's a fresh process) — the calling agent needs to
 *    re-run tabs_context/select_browser after a respawn, same as it
 *    would need to after any fresh connection. The proxy labels the
 *    response so that's visible.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "node:fs";

function resolveClaudeBin() {
  const envBin = process.env.RAPP_CHROME_CLAUDE_BIN;
  if (envBin && fs.existsSync(envBin)) return envBin;
  const home = process.env.HOME || "";
  const localBin = `${home}/.local/bin/claude`;
  if (fs.existsSync(localBin)) return localBin;
  return "claude"; // fall back to PATH resolution
}

const CLAUDE_BIN = process.argv[2] || resolveClaudeBin();
const MAX_RESPAWNS_PER_WINDOW = 3;
const RESPAWN_WINDOW_MS = 2 * 60 * 1000;
const SAME_CONNECTION_RETRY_DELAY_MS = 1500;

const HUNG_PATTERNS = [
  /did not respond in time/i,
  /script injection timed out/i,
  /unresponsive/i,
  /timed out after \d+ms/i,
  /CDP sendCommand.*timed out/i,
  /renderer may be frozen/i,
];
const PERMISSION_PATTERNS = [/permission denied/i, /not connected/i];

function log(...args) {
  // Visible in the parent process's stderr (not sent over the MCP stdio
  // channel), so this never corrupts protocol traffic.
  console.error("[rapp-chrome-supervisor]", ...args);
}

let client = null;
let transport = null;
let respawnTimestamps = [];

async function connectChild() {
  if (client) {
    try {
      await client.close();
    } catch {
      /* already dead, ignore */
    }
  }
  transport = new StdioClientTransport({
    command: CLAUDE_BIN,
    args: ["--claude-in-chrome-mcp"],
  });
  client = new Client({ name: "rapp-chrome-supervisor", version: "1.0.0" }, { capabilities: {} });
  await client.connect(transport);
  log("connected to real Claude-in-Chrome bridge:", CLAUDE_BIN);
}

function canRespawn() {
  const now = Date.now();
  respawnTimestamps = respawnTimestamps.filter((t) => now - t < RESPAWN_WINDOW_MS);
  return respawnTimestamps.length < MAX_RESPAWNS_PER_WINDOW;
}

async function respawn() {
  respawnTimestamps.push(Date.now());
  log(`respawning underlying bridge (attempt ${respawnTimestamps.length} in current window)`);
  await connectChild();
}

function extractErrorText(result, thrownErr) {
  if (thrownErr) return String(thrownErr && thrownErr.message ? thrownErr.message : thrownErr);
  if (result && result.isError && Array.isArray(result.content)) {
    return result.content.map((c) => c.text || "").join(" ");
  }
  return "";
}

function looksHung(text) {
  return HUNG_PATTERNS.some((re) => re.test(text));
}
function looksLikePermission(text) {
  return PERMISSION_PATTERNS.some((re) => re.test(text));
}

const server = new Server(
  { name: "rapp-copilot-in-chrome", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  if (!client) await connectChild();
  try {
    return await client.listTools();
  } catch (err) {
    if (canRespawn()) {
      await respawn();
      return await client.listTools();
    }
    throw err;
  }
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (!client) await connectChild();
  const { name, arguments: args } = request.params;

  const attempt = async () => client.callTool({ name, arguments: args });

  // First attempt.
  let result, thrown;
  try {
    result = await attempt();
  } catch (err) {
    thrown = err;
  }
  let errText = extractErrorText(result, thrown);

  if (!errText) return result; // clean success

  if (looksLikePermission(errText)) {
    // A human needs to act (approve a prompt, select a browser). Retrying
    // or respawning won't fix this — surface it immediately.
    return finalize(result, thrown, errText, false);
  }

  if (!looksHung(errText)) {
    // Some other tool-level error (bad selector, page 404, etc.) — not
    // our job to retry; hand it back as-is.
    return finalize(result, thrown, errText, false);
  }

  // Looks like a hang/timeout. Retry once on the same connection first —
  // covers a transiently busy page without paying for a full respawn.
  log("hung-looking failure, retrying once on same connection:", errText.slice(0, 200));
  await sleep(SAME_CONNECTION_RETRY_DELAY_MS);
  result = undefined;
  thrown = undefined;
  try {
    result = await attempt();
  } catch (err) {
    thrown = err;
  }
  errText = extractErrorText(result, thrown);
  if (!errText) return result;
  if (!looksHung(errText)) return finalize(result, thrown, errText, false);

  // Still hung. Respawn the underlying bridge process and retry once more.
  if (!canRespawn()) {
    log("respawn budget exhausted for this window, surfacing error");
    return finalize(result, thrown, errText, false, true);
  }
  await respawn();
  result = undefined;
  thrown = undefined;
  try {
    result = await attempt();
  } catch (err) {
    thrown = err;
  }
  errText = extractErrorText(result, thrown);
  return finalize(result, thrown, errText, true);
});

function finalize(result, thrown, errText, didRespawn, budgetExhausted = false) {
  if (!errText) {
    if (didRespawn) {
      // Success after a respawn: tab/browser selection state was reset.
      const note = {
        type: "text",
        text: "[rapp-chrome-supervisor] note: the browser bridge was automatically restarted before this call succeeded — tab IDs and browser selection from before the restart are gone. Call tabs_context_mcp again.",
      };
      return { content: [note, ...(result.content || [])] };
    }
    return result;
  }
  if (thrown) throw thrown;
  if (budgetExhausted) {
    result.content = [
      {
        type: "text",
        text: `${errText}\n\n[rapp-chrome-supervisor] automatic restart budget exhausted (${MAX_RESPAWNS_PER_WINDOW} restarts within ${RESPAWN_WINDOW_MS / 1000}s) — this looks like a persistent problem, not a transient hang. Manual investigation needed.`,
      },
    ];
  }
  return result;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const serverTransport = new StdioServerTransport();
await server.connect(serverTransport);

process.on("SIGINT", async () => {
  if (client) await client.close();
  process.exit(0);
});
