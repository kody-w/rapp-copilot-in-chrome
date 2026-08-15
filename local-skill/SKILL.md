---
name: rappter-chrome-local
description: Drive the user's real, logged-in Edge or Chrome through the vendorless local Rappter bridge.
---

# Rappter Chrome Local

Use this skill for browser work in the user's real authenticated Chromium
profile. It is not a headless browser.

## Start here

Always call `tabs_context_mcp` first. It returns the tab IDs required by the
other tools. Set `createIfEmpty: true` when an empty profile is acceptable.

Prefer `browser_batch` for ordered multi-step work.

## Tools

- `tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp`
- `navigate`
- `get_page_text`, `read_page`
- `form_input`
- `computer` (`click`, `type`, `activate`, `screenshot`)
- `javascript_tool`
- `browser_batch`
- `list_connected_browsers`

## Safety

This is the user's real browser and real authenticated identity.

- Confirm before sending, purchasing, publishing, deleting, or submitting.
- Read the page after an irreversible action and verify the result.
- Never treat a click without readback as proof of completion.
- Use `javascript_tool` only when the ordinary read/click/type tools cannot
  express the action.

## Google Voice

The local runtime also includes `gvoice.py` and `voice_assistant.py`.
Google Voice sends are account-locked and are only successful after the sent
text appears as an outgoing message in the thread.
