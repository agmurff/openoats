"""Export session notes to Notion as a child page under a parent page.

Markdown notes are converted into Notion blocks (headings, bullets, paragraphs).
Notion limits rich_text content to 2000 chars and children to 100 blocks per
request, so long content is split and appended in batches.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
MAX_TEXT = 2000
MAX_BLOCKS_PER_REQUEST = 100


def _rich_text(content: str) -> list[dict]:
    """Markdown inline bold (**text**) -> bold spans; chunk to Notion's
    2000-char per-span limit."""
    import re
    spans: list[dict] = []
    for part in re.split(r"(\*\*[^*]+\*\*)", content):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        text = part[2:-2] if bold else part
        for i in range(0, len(text), MAX_TEXT):
            span: dict = {"type": "text", "text": {"content": text[i:i + MAX_TEXT]}}
            if bold:
                span["annotations"] = {"bold": True}
            spans.append(span)
    return spans or [{"type": "text", "text": {"content": ""}}]


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Best-effort markdown -> Notion blocks. Handles headings, bullets, paragraphs."""
    blocks: list[dict] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _rich_text(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich_text(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped[:2] in ("- ", "* ") or stripped.startswith("• "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich_text(stripped[2:].lstrip())}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich_text(line)}})
    return blocks


def transcript_to_blocks(utterances) -> list[dict]:
    """Render utterances as Notion paragraph blocks: '**YOU** 10:42  text'."""
    blocks: list[dict] = [
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": _rich_text("Transcript")}}
    ]
    for u in utterances:
        speaker = "YOU" if getattr(u, "speaker", "") == "you" else "THEM"
        ts = u.timestamp.strftime("%H:%M") if getattr(u, "timestamp", None) else ""
        text = getattr(u, "text", "")
        # One paragraph per utterance, with bold speaker label
        rich = [
            {"type": "text", "text": {"content": f"{speaker} "},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"{ts}  " if ts else ""}},
        ]
        # Body of the utterance, split if needed
        for span in _rich_text(text):
            rich.append(span)
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": rich}})
    return blocks


class NotionExporter:
    def __init__(self, api_key: str, parent_page_id: str):
        self._api_key = api_key
        self._parent = parent_page_id

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Notion-Version": VERSION,
        }

    async def create_page(self, title: str, markdown: str, utterances=None) -> str:
        """Create a child page under the parent and fill it with the notes,
        optionally appending the full transcript. Returns the new page id."""
        if not self._api_key or not self._parent:
            raise ValueError("Notion API key and parent page id are required")

        blocks = markdown_to_blocks(markdown)
        if utterances:
            blocks.extend(transcript_to_blocks(utterances))
        first_batch, rest = blocks[:MAX_BLOCKS_PER_REQUEST], blocks[MAX_BLOCKS_PER_REQUEST:]

        body = {
            "parent": {"type": "page_id", "page_id": self._parent},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": first_batch,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{API}/pages", json=body, headers=self._headers())
            if not (200 <= resp.status_code < 300):
                raise RuntimeError(f"Notion create page HTTP {resp.status_code}: {resp.text}")
            page_id = resp.json()["id"]

            # Append any overflow blocks in 100-block batches.
            for i in range(0, len(rest), MAX_BLOCKS_PER_REQUEST):
                batch = rest[i:i + MAX_BLOCKS_PER_REQUEST]
                r = await client.patch(
                    f"{API}/blocks/{page_id}/children",
                    json={"children": batch}, headers=self._headers(),
                )
                if not (200 <= r.status_code < 300):
                    logger.warning("Notion append overflow blocks HTTP %s: %s", r.status_code, r.text)
                    break

        logger.info("Notion child page created: %s", page_id)
        return page_id
