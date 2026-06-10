_STYLE = (
    "Be ruthless about brevity. Bullets only, no preamble, no closing remarks, "
    "no restating the transcript. Max 8 words of context per bullet beyond the fact itself. "
    "Skip any section with nothing to report (omit the heading entirely). "
    "Never invent owners, dates, or decisions that weren't said."
)

TEMPLATES: list[tuple[str, str]] = [
    (
        "Generic",
        (
            "Produce tight meeting notes in this exact structure:\n"
            "## Decisions\n- (each decision, one line)\n"
            "## Action Items\n- Owner: task (due date if stated)\n"
            "## Key Points\n- (max 5 bullets, only things someone would need to recall later)\n"
            f"\n{_STYLE}"
        ),
    ),
    (
        "1:1",
        (
            "Produce tight 1:1 notes:\n"
            "## Feedback\n- (given and received, one line each)\n"
            "## Action Items\n- Owner: task\n"
            "## Follow-ups\n- (only if explicitly agreed)\n"
            f"\n{_STYLE}"
        ),
    ),
    (
        "Customer Discovery",
        (
            "Produce tight discovery-call notes:\n"
            "## Pain Points\n- (one line each)\n"
            "## Product Signals\n- (feature asks, willingness to pay, objections)\n"
            "## Quotes\n- (max 2, verbatim, only if genuinely revealing)\n"
            "## Next Steps\n- Owner: task\n"
            f"\n{_STYLE}"
        ),
    ),
    (
        "Hiring Interview",
        (
            "Produce tight interview notes:\n"
            "## Strengths\n- (one line each)\n"
            "## Concerns\n- (one line each)\n"
            "## Recommendation\n- Hire / no-hire / lean, with one-line rationale\n"
            f"\n{_STYLE}"
        ),
    ),
    (
        "Stand-Up",
        (
            "Produce tight stand-up notes:\n"
            "## By Person\n- Name: did X, next Y, blocked on Z (omit empty parts)\n"
            "## Cross-team Actions\n- Owner: task\n"
            f"\n{_STYLE}"
        ),
    ),
    (
        "Weekly Meeting",
        (
            "Produce tight weekly-meeting notes:\n"
            "## Decisions\n- (one line each)\n"
            "## Action Items\n- Owner: task (due date if stated)\n"
            "## Open Questions\n- (only if left unresolved)\n"
            f"\n{_STYLE}"
        ),
    ),
]

TEMPLATE_NAMES = [name for name, _ in TEMPLATES]


def get_prompt(template_name: str) -> str:
    for name, prompt in TEMPLATES:
        if name == template_name:
            return prompt
    return TEMPLATES[0][1]
