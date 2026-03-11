#!/usr/bin/env python3
"""
CISO Approval Triage Bot

Monitors a Slack channel for messages containing Jira or Confluence links,
classifies them via Claude API, and posts appropriate responses.

Runs as a macOS launchd service at a configurable interval.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ATLASSIAN_EMAIL = os.environ["ATLASSIAN_EMAIL"]
ATLASSIAN_API_TOKEN = os.environ["ATLASSIAN_API_TOKEN"]
ATLASSIAN_DOMAIN = os.environ["ATLASSIAN_DOMAIN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]
CISO_SLACK_ID = os.environ["CISO_SLACK_ID"]
BOT_SLACK_ID = os.environ.get("BOT_SLACK_ID", "")

# Jira project key prefix — change to match your Jira project (e.g., "SEC", "IT")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SEC")

STATE_FILE = BASE_DIR / "processed_requests.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Bot signature appended to messages
BOT_SIGNATURE = "Sent using"

# Claude model to use for classification
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# Regex patterns for extracting Jira keys and Confluence page IDs
JIRA_KEY_PATTERN = re.compile(rf"\b({JIRA_PROJECT_KEY}-\d+)\b")
JIRA_URL_PATTERN = re.compile(
    rf"https?://[a-zA-Z0-9.-]+\.atlassian\.net/browse/({JIRA_PROJECT_KEY}-\d+)"
)
CONFLUENCE_PAGE_ID_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9.-]+\.atlassian\.net/wiki/spaces/[^/]+/pages/(\d+)"
)
CONFLUENCE_SHORT_LINK_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9.-]+\.atlassian\.net/wiki/x/([A-Za-z0-9_-]+)"
)
# Also match /pages/PAGEID/ without the spaces segment
CONFLUENCE_PAGES_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9.-]+\.atlassian\.net/wiki/[^ ]*?/pages/(\d+)"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ciso-bot")

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

slack_client = WebClient(token=SLACK_BOT_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ATLASSIAN_AUTH = (ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN)
ATLASSIAN_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """Load processed requests state from disk."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning("Failed to load state file, starting fresh: %s", e)
    return {"processed": {}, "waiting_for_info": {}}


def save_state(state: dict) -> None:
    """Persist state to disk atomically."""
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------


def fetch_channel_history(limit: int = 20) -> list[dict]:
    """Fetch recent messages from the monitored channel."""
    try:
        resp = slack_client.conversations_history(
            channel=SLACK_CHANNEL_ID, limit=limit
        )
        return resp.get("messages", [])
    except SlackApiError as e:
        log.error("Failed to fetch channel history: %s", e)
        return []


def fetch_thread_replies(thread_ts: str) -> list[dict]:
    """Fetch all replies in a Slack thread."""
    try:
        resp = slack_client.conversations_replies(
            channel=SLACK_CHANNEL_ID, ts=thread_ts
        )
        return resp.get("messages", [])
    except SlackApiError as e:
        log.error("Failed to fetch thread replies for %s: %s", thread_ts, e)
        return []


def thread_has_bot_reply(replies: list[dict]) -> bool:
    """Check if the bot has already replied in the thread."""
    for msg in replies:
        if msg.get("user") == BOT_SLACK_ID:
            return True
        text = msg.get("text", "")
        if BOT_SIGNATURE in text:
            return True
    return False


def thread_has_ciso_approval(replies: list[dict]) -> bool:
    """Check if the CISO has already replied with an approval."""
    for msg in replies:
        if msg.get("user") == CISO_SLACK_ID:
            text = msg.get("text", "").lower()
            if any(
                word in text
                for word in ["approved", "approve", "lgtm", "go ahead", "looks good"]
            ):
                return True
    return False


def post_slack_message(thread_ts: str, text: str) -> bool:
    """Post a message in a Slack thread."""
    try:
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            thread_ts=thread_ts,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("Posted Slack message in thread %s", thread_ts)
        return True
    except SlackApiError as e:
        log.error("Failed to post Slack message in thread %s: %s", thread_ts, e)
        return False


def get_requestor_slack_id(message: dict) -> str:
    """Extract the Slack user ID of the person who posted the message."""
    return message.get("user", "")


# ---------------------------------------------------------------------------
# Atlassian helpers
# ---------------------------------------------------------------------------


def fetch_jira_ticket(key: str) -> dict | None:
    """Fetch a Jira ticket by key via REST API v3."""
    url = f"https://{ATLASSIAN_DOMAIN}/rest/api/3/issue/{key}"
    try:
        resp = requests.get(
            url, auth=ATLASSIAN_AUTH, headers=ATLASSIAN_HEADERS, timeout=30
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Failed to fetch Jira ticket %s: %s", key, e)
        return None


def fetch_confluence_page(page_id: str) -> dict | None:
    """Fetch a Confluence page by ID via REST API v2."""
    url = f"https://{ATLASSIAN_DOMAIN}/wiki/api/v2/pages/{page_id}"
    params = {"body-format": "storage"}
    try:
        resp = requests.get(
            url, auth=ATLASSIAN_AUTH, headers=ATLASSIAN_HEADERS, params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Failed to fetch Confluence page %s: %s", page_id, e)
        return None


def resolve_confluence_short_link(code: str) -> str | None:
    """Try to resolve a Confluence short link (/wiki/x/CODE) to a page ID."""
    url = f"https://{ATLASSIAN_DOMAIN}/wiki/x/{code}"
    try:
        resp = requests.head(
            url, auth=ATLASSIAN_AUTH, allow_redirects=True, timeout=30
        )
        final_url = resp.url
        match = CONFLUENCE_PAGES_PATTERN.search(final_url)
        if match:
            return match.group(1)
        match = CONFLUENCE_PAGE_ID_PATTERN.search(final_url)
        if match:
            return match.group(1)
    except requests.RequestException as e:
        log.warning("Failed to resolve Confluence short link %s: %s", code, e)
    return None


def post_jira_comment(key: str, comment_text: str) -> bool:
    """Post a comment on a Jira ticket using ADF format."""
    url = f"https://{ATLASSIAN_DOMAIN}/rest/api/3/issue/{key}/comment"
    body = {
        "body": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment_text}],
                }
            ],
        }
    }
    try:
        resp = requests.post(
            url,
            auth=ATLASSIAN_AUTH,
            headers=ATLASSIAN_HEADERS,
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Posted Jira comment on %s", key)
        return True
    except requests.RequestException as e:
        log.error("Failed to post Jira comment on %s: %s", key, e)
        return False


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_jira_keys(text: str) -> list[str]:
    """Extract all Jira ticket keys from message text."""
    keys = set()
    for m in JIRA_URL_PATTERN.finditer(text):
        keys.add(m.group(1))
    for m in JIRA_KEY_PATTERN.finditer(text):
        keys.add(m.group(1))
    return sorted(keys)


def extract_confluence_page_ids(text: str) -> list[str]:
    """Extract Confluence page IDs from message text."""
    ids = set()
    for m in CONFLUENCE_PAGE_ID_PATTERN.finditer(text):
        ids.add(m.group(1))
    for m in CONFLUENCE_PAGES_PATTERN.finditer(text):
        ids.add(m.group(1))
    return sorted(ids)


def extract_confluence_short_codes(text: str) -> list[str]:
    """Extract Confluence short link codes from message text."""
    codes = set()
    for m in CONFLUENCE_SHORT_LINK_PATTERN.finditer(text):
        codes.add(m.group(1))
    return sorted(codes)


def has_relevant_links(text: str) -> bool:
    """Check if message contains Jira or Confluence links."""
    return bool(
        extract_jira_keys(text)
        or extract_confluence_page_ids(text)
        or extract_confluence_short_codes(text)
    )


# ---------------------------------------------------------------------------
# Jira data formatting
# ---------------------------------------------------------------------------


def extract_adf_text(node: dict | list | str | None) -> str:
    """Recursively extract plain text from an ADF (Atlassian Document Format) node."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(extract_adf_text(item) for item in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        content = node.get("content", [])
        return " ".join(extract_adf_text(child) for child in content)
    return ""


def format_jira_for_claude(ticket: dict) -> str:
    """Format Jira ticket data into a readable string for Claude."""
    fields = ticket.get("fields", {})
    key = ticket.get("key", "UNKNOWN")

    summary = fields.get("summary", "N/A")

    description_raw = fields.get("description")
    if isinstance(description_raw, dict):
        description = extract_adf_text(description_raw)
    elif isinstance(description_raw, str):
        description = description_raw
    else:
        description = "No description provided"

    status = "N/A"
    if fields.get("status"):
        status = fields["status"].get("name", "N/A")

    assignee = "Unassigned"
    if fields.get("assignee"):
        assignee = fields["assignee"].get("displayName", "Unassigned")

    reporter = "Unknown"
    reporter_account_id = ""
    if fields.get("reporter"):
        reporter = fields["reporter"].get("displayName", "Unknown")
        reporter_account_id = fields["reporter"].get("accountId", "")

    priority = "N/A"
    if fields.get("priority"):
        priority = fields["priority"].get("name", "N/A")

    issue_type = "N/A"
    if fields.get("issuetype"):
        issue_type = fields["issuetype"].get("name", "N/A")

    labels = ", ".join(fields.get("labels", [])) or "None"

    created = fields.get("created", "N/A")
    updated = fields.get("updated", "N/A")

    components = ", ".join(
        c.get("name", "") for c in fields.get("components", [])
    ) or "None"

    lines = [
        f"Jira Ticket: {key}",
        f"Type: {issue_type}",
        f"Summary: {summary}",
        f"Description: {description}",
        f"Status: {status}",
        f"Priority: {priority}",
        f"Assignee: {assignee}",
        f"Reporter: {reporter} (accountId: {reporter_account_id})",
        f"Labels: {labels}",
        f"Components: {components}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]
    return "\n".join(lines)


def format_confluence_for_claude(page: dict) -> str:
    """Format Confluence page data into a readable string for Claude."""
    title = page.get("title", "N/A")
    page_id = page.get("id", "N/A")
    status = page.get("status", "N/A")

    body_content = ""
    body = page.get("body", {})
    if "storage" in body:
        body_content = body["storage"].get("value", "")
    elif "view" in body:
        body_content = body["view"].get("value", "")

    body_text = re.sub(r"<[^>]+>", " ", body_content)
    body_text = re.sub(r"\s+", " ", body_text).strip()
    if len(body_text) > 3000:
        body_text = body_text[:3000] + "... [truncated]"

    lines = [
        f"Confluence Page ID: {page_id}",
        f"Title: {title}",
        f"Status: {status}",
        f"Content: {body_text or 'No content available'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API triage
# ---------------------------------------------------------------------------

# Load the triage prompt from the prompts directory
TRIAGE_PROMPT_FILE = BASE_DIR / "prompts" / "triage_system_prompt.md"


def _load_triage_prompt() -> str:
    """Load the triage system prompt from file, with variable substitution."""
    if TRIAGE_PROMPT_FILE.exists():
        template = TRIAGE_PROMPT_FILE.read_text()
        return template.replace("{CISO_SLACK_ID}", CISO_SLACK_ID).replace(
            "{BOT_SLACK_ID}", BOT_SLACK_ID
        )
    # Fallback inline prompt
    return (
        "You are the CISO Approval Triage Bot. Classify the request as "
        "LOW, MEDIUM, HIGH, or MISSING_INFO. Return valid JSON."
    )


TRIAGE_SYSTEM_PROMPT = _load_triage_prompt()


def classify_with_claude(
    ticket_text: str,
    ticket_key: str,
    requestor_slack_id: str,
    source_type: str = "jira",
) -> dict | None:
    """Send ticket data to Claude API and parse the classification response."""
    user_message = (
        f"Please triage the following {source_type} request.\n\n"
        f"Requestor Slack ID: {requestor_slack_id}\n"
        f"Ticket/Page Key: {ticket_key}\n\n"
        f"--- BEGIN DATA ---\n{ticket_text}\n--- END DATA ---"
    )

    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=TRIAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        result = json.loads(raw)
        log.info(
            "Claude classified %s as %s (%s)",
            ticket_key,
            result.get("classification"),
            result.get("decision"),
        )
        return result
    except json.JSONDecodeError as e:
        log.error("Failed to parse Claude response as JSON: %s — raw: %s", e, raw)
        return None
    except anthropic.APIError as e:
        log.error("Claude API error: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected error calling Claude: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main processing logic
# ---------------------------------------------------------------------------


def process_message(
    message: dict, state: dict
) -> None:
    """Process a single Slack message for triage."""
    msg_ts = message.get("ts", "")
    text = message.get("text", "")
    requestor_slack_id = get_requestor_slack_id(message)

    # Skip if already fully processed
    if msg_ts in state["processed"] and state["processed"][msg_ts].get("status") == "done":
        log.debug("Skipping already-processed message %s", msg_ts)
        return

    # Check if this is a waiting_for_info message that got new replies
    is_waiting = msg_ts in state.get("waiting_for_info", {})

    if not has_relevant_links(text):
        return

    # Fetch thread replies
    replies = fetch_thread_replies(msg_ts)

    # If already handled (bot replied or CISO approved), mark done and skip
    if thread_has_bot_reply(replies) and not is_waiting:
        state["processed"][msg_ts] = {
            "status": "done",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return

    if thread_has_ciso_approval(replies):
        state["processed"][msg_ts] = {
            "status": "done",
            "reason": "ciso_approved",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state.get("waiting_for_info", {}).pop(msg_ts, None)
        return

    # If waiting for info, check if there are new replies since our last post
    if is_waiting:
        waiting_info = state["waiting_for_info"][msg_ts]
        last_bot_ts = waiting_info.get("bot_reply_ts", "0")
        new_replies = [
            r for r in replies
            if r.get("ts", "0") > last_bot_ts
            and r.get("user") != BOT_SLACK_ID
        ]
        if not new_replies:
            log.debug("Still waiting for info on %s, no new replies", msg_ts)
            return
        log.info("New replies found on waiting message %s, re-evaluating", msg_ts)

    # Extract Jira keys
    jira_keys = extract_jira_keys(text)
    confluence_page_ids = extract_confluence_page_ids(text)
    confluence_short_codes = extract_confluence_short_codes(text)

    # Resolve short codes to page IDs
    for code in confluence_short_codes:
        resolved_id = resolve_confluence_short_link(code)
        if resolved_id and resolved_id not in confluence_page_ids:
            confluence_page_ids.append(resolved_id)
            log.info("Resolved Confluence short link /x/%s to page ID %s", code, resolved_id)
        elif not resolved_id:
            log.warning(
                "Could not resolve Confluence short link /x/%s — will flag as needing clarification",
                code,
            )

    # Process each Jira ticket
    for key in jira_keys:
        _process_jira_ticket(key, msg_ts, requestor_slack_id, state)

    # Process each Confluence page
    for page_id in confluence_page_ids:
        _process_confluence_page(page_id, msg_ts, requestor_slack_id, text, state)

    # If we had unresolved short codes and no other links were processed, flag it
    if (
        not jira_keys
        and not confluence_page_ids
        and confluence_short_codes
    ):
        msg = (
            f"Hi <@{requestor_slack_id}>, I found a Confluence short link but couldn't "
            f"resolve it to a page. Could you provide the full page URL?\n\n"
            f"*{BOT_SIGNATURE}* <@{BOT_SLACK_ID}|Claude>"
        )
        post_slack_message(msg_ts, msg)
        state["processed"][msg_ts] = {
            "status": "done",
            "reason": "unresolved_short_link",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _process_jira_ticket(
    key: str, msg_ts: str, requestor_slack_id: str, state: dict
) -> None:
    """Fetch, classify, and act on a single Jira ticket."""
    log.info("Processing Jira ticket %s from message %s", key, msg_ts)

    ticket = fetch_jira_ticket(key)
    if not ticket:
        log.warning("Could not fetch Jira ticket %s, skipping", key)
        return

    ticket_text = format_jira_for_claude(ticket)

    reporter_account_id = ""
    fields = ticket.get("fields", {})
    if fields.get("reporter"):
        reporter_account_id = fields["reporter"].get("accountId", "")

    result = classify_with_claude(ticket_text, key, requestor_slack_id, "jira")
    if not result:
        log.error("Claude classification failed for %s, skipping", key)
        return

    _act_on_classification(
        result, key, msg_ts, requestor_slack_id, reporter_account_id, state
    )


def _process_confluence_page(
    page_id: str,
    msg_ts: str,
    requestor_slack_id: str,
    original_text: str,
    state: dict,
) -> None:
    """Fetch, classify, and act on a single Confluence page."""
    log.info("Processing Confluence page %s from message %s", page_id, msg_ts)

    page = fetch_confluence_page(page_id)
    if not page:
        log.warning("Could not fetch Confluence page %s, skipping", page_id)
        return

    page_text = format_confluence_for_claude(page)
    page_title = page.get("title", f"Page-{page_id}")

    result = classify_with_claude(
        page_text, page_title, requestor_slack_id, "confluence"
    )
    if not result:
        log.error("Claude classification failed for Confluence page %s, skipping", page_id)
        return

    decision = result.get("decision", "")
    classification = result.get("classification", "")
    slack_msg = result.get("slack_message", "")

    if not slack_msg:
        slack_msg = (
            f"Reviewed Confluence page *{page_title}* (ID: {page_id}). "
            f"Classification: {classification}.\n\n"
            f"*{BOT_SIGNATURE}* <@{BOT_SLACK_ID}|Claude>"
        )

    success = post_slack_message(msg_ts, slack_msg)
    if success:
        state["processed"][msg_ts] = {
            "status": "done",
            "confluence_page_id": page_id,
            "classification": classification,
            "decision": decision,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _act_on_classification(
    result: dict,
    ticket_key: str,
    msg_ts: str,
    requestor_slack_id: str,
    reporter_account_id: str,
    state: dict,
) -> None:
    """Take action based on the Claude classification result."""
    decision = result.get("decision", "")
    classification = result.get("classification", "")
    slack_msg = result.get("slack_message", "")
    jira_questions = result.get("jira_questions", [])

    if not slack_msg:
        slack_msg = (
            f"Triage result for *{ticket_key}*: {classification} — {decision}.\n\n"
            f"*{BOT_SIGNATURE}* <@{BOT_SLACK_ID}|Claude>"
        )

    if decision == "APPROVE":
        success = post_slack_message(msg_ts, slack_msg)
        if success:
            state["processed"][msg_ts] = {
                "status": "done",
                "ticket": ticket_key,
                "classification": classification,
                "decision": decision,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            state.get("waiting_for_info", {}).pop(msg_ts, None)

    elif decision == "MANUAL_REVIEW_REQUIRED":
        success = post_slack_message(msg_ts, slack_msg)
        if success:
            state["processed"][msg_ts] = {
                "status": "done",
                "ticket": ticket_key,
                "classification": classification,
                "decision": decision,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            state.get("waiting_for_info", {}).pop(msg_ts, None)

    elif decision == "NEEDS_INFO":
        success = post_slack_message(msg_ts, slack_msg)

        if jira_questions:
            questions_text = "\n".join(
                f"- {q}" for q in jira_questions
            )
            jira_comment = (
                f"CISO Approval Triage Bot — additional information needed:\n\n"
                f"{questions_text}\n\n"
                f"Please update this ticket with the requested details."
            )
            if reporter_account_id:
                jira_comment = (
                    f"[~accountId:{reporter_account_id}] " + jira_comment
                )
            post_jira_comment(ticket_key, jira_comment)

        if success:
            bot_reply_ts = ""
            replies = fetch_thread_replies(msg_ts)
            for r in reversed(replies):
                if r.get("user") == BOT_SLACK_ID or BOT_SIGNATURE in r.get("text", ""):
                    bot_reply_ts = r.get("ts", "")
                    break

            state["waiting_for_info"][msg_ts] = {
                "ticket": ticket_key,
                "classification": classification,
                "decision": decision,
                "bot_reply_ts": bot_reply_ts,
                "questions": jira_questions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            state["processed"][msg_ts] = {
                "status": "waiting_for_info",
                "ticket": ticket_key,
                "classification": classification,
                "decision": decision,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
    else:
        log.warning("Unknown decision '%s' for %s, posting message anyway", decision, ticket_key)
        post_slack_message(msg_ts, slack_msg)
        state["processed"][msg_ts] = {
            "status": "done",
            "ticket": ticket_key,
            "classification": classification,
            "decision": decision,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Main bot execution loop (single pass)."""
    start = time.time()
    log.info("=== CISO Triage Bot run starting ===")

    state = load_state()

    if "waiting_for_info" not in state:
        state["waiting_for_info"] = {}

    messages = fetch_channel_history(limit=20)
    if not messages:
        log.info("No messages found in channel")
        save_state(state)
        return

    log.info("Fetched %d messages from channel", len(messages))

    for message in messages:
        try:
            process_message(message, state)
        except Exception as e:
            msg_ts = message.get("ts", "unknown")
            log.error("Error processing message %s: %s", msg_ts, e, exc_info=True)

    save_state(state)
    elapsed = time.time() - start
    log.info("=== CISO Triage Bot run complete (%.1fs) ===", elapsed)


if __name__ == "__main__":
    main()
