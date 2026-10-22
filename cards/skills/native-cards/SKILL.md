---
name: native-cards
description: Answer with native cards (charts, metrics, tables, lists, timelines, checklists, forms, choices) in the Agents CS app when structure reads better than prose.
---

# Native cards in Agents CS

The person reads your replies in Agents CS, a Messages-style app on iPhone, iPad and Mac. The
`hermes_card_*` tools draw a native card below your reply instead of a wall of text or a Markdown
table. Channels that can't draw cards (Telegram, Slack, scheduled-job deliveries) get the card's
plain-text version automatically, so a card never breaks a reply.

## When to use a card

Use one when the answer has shape:

| The answer is… | Tool |
| --- | --- |
| A few key numbers (totals, KPIs, scores, balances) | `hermes_card_metrics` |
| A trend over time, a comparison, a breakdown of a whole | `hermes_card_chart` (line/area, bar, pie) |
| Rows with the same columns (transactions, options compared, a schedule) | `hermes_card_table` |
| A set of items or results | `hermes_card_list` |
| Steps in order, with progress | `hermes_card_timeline` |
| Things to do or check off | `hermes_card_checklist` |
| A titled status note (a result, a warning, a done) | `hermes_card_summary` |
| You need several details from the person | `hermes_card_form` |
| You need the person to decide (at most 3 options; Other is added for you) | `hermes_card_selection` |
| You drafted an email or message on any service for the person to send | `hermes_card_message` |

Don't use a card for conversation, explanations, opinions, or one-line answers. One card per reply is
usual; when there are several decisions, send each as its own card in the deck. Never more than ten.

## How

1. Call the tool once with complete, final data. Titles are short ("Spending this week", not
   "Here is a card showing…"). Values are already formatted for reading: `"$1,240"`, `"98%"`,
   `"3h 20m"`. Tone is `positive`, `warning`, `negative` or `neutral`; use it sparingly for meaning,
   never decoration.
2. Write your reply as usual, with at most one short sentence about the card ("Here's the week.").
   Don't repeat the card's numbers or rows in prose, and don't copy the card JSON unless the tool
   result gives you `display_markdown` and tells you to include it.
3. If the tool returns an `error`, fix the arguments it names and call it again, or answer in text.

## Decisions: at most three options, plus Other

Whenever you need the person to decide something, use `hermes_card_selection` with `mode: single`:

- **At most 3 options.** The app always adds **Other** (a typed answer) and an **Add details** box, so
  never add your own "Other", "Something else" or "None of these" option. More than 3 is refused.
- Put the option you recommend first and mark it `"recommended": true` (at most one).
- Labels are short answers the person could say out loud ("Refresh it now for Q4"); the reason goes
  in `detail` (one line) and the background in `description`, not in the labels.
- One decision per card. Several decisions in one reply are fine: each becomes its own card in a deck
  the person works through, one at a time.
- The answer comes back as one of your option labels, their own words (Other), or **Skipped**. Skipped
  means "not now": keep the existing request deferred without asking again next run; it is neither approval nor a no. Reopen only on their request, material new facts or an agreed reminder. If the person
  sends a message instead of answering, the open cards arrive as Skipped with that message.
- Don't build a decision as a form with a menu field. Forms are for collecting several details; their
  menus also hold at most 3 options.

## Messages you draft: always ask first

Whenever you write an email or a message on any service (Upwork, Teams, iMessage, Slack, etc.) that will go out under the person's name
(a reply, a follow-up, an introduction, anything), show it with `hermes_card_message` before it goes
anywhere. This is not optional and not only for "important" messages: every drafted message, every time.

- All non-email messages use the same generic approval card: `to` and `body`. Set `channel` to the
  service name (for example `upwork`, `teams`, `imessage`) or omit it for a generic message. Never
  block because a service is missing from a channel list; there is no channel whitelist. Email alone
  uses `channel: email` and may include a subject. Approval does not itself execute a send or authorize
  an offer, contract, payment or any action beyond the exact message.
- Put the whole message in `body`, exactly as it will be sent: greeting, sign-off, everything. Recipients
  in `to` as the person knows them ("Morgan Goh <morgan@example.com>"); `subject` for email only;
  `thread` for the email thread or Teams chat/channel it joins; `from` for the account, when you know it.
- Title it as the question: "Send this to Morgan?", "Reply to the design channel?".
- Then end your turn. Do not send, schedule, or save it as a draft in their mailbox until they answer.
- **Send** (`selected: ["send"]`, no `other`): send it as written. **Edited** (`edited: true`, with Send): send their `body`
  and `subject` exactly, never your version. **Other** (`other`): their notes; don't send, even if edits are attached;
  rewrite the draft to follow them and show the new draft with a new `hermes_card_message`, then
  end your turn. **Don't send** (`selected: ["dont_send"]`): drop it, and
  don't ask again unless they bring it up. **Skipped**: decide later; preserve the draft without recreating its card.
- After sending, say in one line that it went and to whom. If sending fails, say so plainly.
- One message per card. Several drafts in one reply are fine: each becomes its own card in the deck.

## Cards inside a Kanban task

When you work a Kanban task (the dispatcher spawned you; `HERMES_KANBAN_TASK` is set), a decision card
(`hermes_card_selection` with `mode: single`, or `hermes_card_message`) is bound to that task
automatically and the person answers it from the task board in the app, not from a chat. The tool result
tells you so in `next_step`:

1. Call `kanban_block(kind="needs_input", reason=<the card's title>)` and stop. Don't poll, don't
   `kanban_complete` or `kanban_request_review` while the card is open (the guard refuses it).
2. The answer arrives as a comment on the task: readable lines, then a `hermes-card-response` block with
   the `card_id` and `values`. Hermes unblocks the task and wakes you; read the latest comments first.
3. If you change the draft before the answer lands (new wording, recipients or subject), show the new
   draft with a new card. The old card is withdrawn and the task board shows only the new one. Showing
   the identical draft again reuses the open card; it is never asked twice.

Approvals survive restarts: the worker context includes the original card and saved answer for this
task and its containment ancestors. Answer comments also include the original payload. Use these
records to recover the approved wording instead of asking again. Later human instructions override
older decisions. An approval is not a delivery receipt: read the latest task comments and verify
whether the action already happened before sending. Multiple approvals do not authorize duplicates.

After carrying out the approved action and recording its receipt, if only another person's reply or
confirmation remains, call `hermes_task_wait(reason="Waiting for …; sent receipt …")` and stop.
This parks the current run without another review request. It does not create a timer or poll inboxes;
resume on the reply or a changed human instruction. Do not use `needs_input` for this external wait,
and do not mark the reservation or other final outcome complete before confirmation arrives.

## Creating tasks in the app

Use the native `kanban_create` tool as usual. When creation-only decomposition is enabled, the plugin
keeps a new top-level task in triage and invokes Hermes's decomposer once. The tool result includes
`creation_planning` and tells you the request has already been made. Do not immediately specify,
promote, decompose or recreate it. Read the task for the result. Subtasks, worker-created tasks,
explicitly blocked/held work, and cron or messaging-channel tasks are excluded. Comments, approvals,
status changes and restarts never request another decomposition. A failed or interrupted attempt is
not automatically retried. Hermes's separate background `auto_decompose` switch must stay off.

## Coordinating a decomposed task

When a comment on a root task (one that was decomposed into children) starts you as its coordinator,
the prompt names the root, the board, the person's comment, the family (root and contained tasks) and
the open cards. Rules:

- Act only on those tasks, through the kanban tools. A tool call naming a task outside the family is
  refused; `kanban_create` must list the root or a family member in `parents`.
- Prefer `kanban_comment` on the child and letting it continue, or `kanban_unblock`, over new tasks.
- A changed draft means the child withdraws its card and makes a new one; never answer a card for the
  person.
- When the request is ambiguous, ask with one `hermes_card_selection` on the root and stop.
- Never send messages or spend money.
- Finish with one short `kanban_comment` on the root saying what you asked whom.

## Forms and other answers

- `hermes_card_form`: up to 12 fields (`text`, `textarea`, `select`, `multi_select`, `toggle`,
  `integer`, `decimal`, `date`); `select` and `multi_select` hold at most 3 options. Mark only what
  you truly need as `required`. After showing a form, end your turn and wait.
- `hermes_card_selection` with `multiple` shows a send button (`submit_label`) for "choose any that apply".
- `hermes_card_checklist` with `submit_label` lets the person tick items and send the result back;
  without it the checklist is display-only.

The person's answer arrives as their next message: readable lines followed by a
`hermes-card-response` block with `card_id` and `values` (option ids for choices, field ids for
forms). Treat it as their reply. For a form, you can also call `hermes_card_form_response` with the
`request_id` from the tool result to read the values Hermes checked against your form.

Every waiting card accepts a comment. Form answers may contain partial fields plus `_comment`;
choices and checklists may contain selections/ticks plus `other`. Treat the supplied values and
comment together as the person's answer, including instructions for a different next step. Ask a
follow-up if you still need information; never invent missing fields or force the same choices again.

## Chart tips

- Line or area for time series; bar for comparing categories; pie only for parts of one whole, 2–8
  slices.
- Up to 6 series of up to 60 points. Keep x labels short ("Mon", "Sep 3", "Q2").
- `unit` goes with every value: currency symbols before (`"$"` → $1,240), others after (`"%"` → 98%,
  `"kg"` → 12 kg).

## Tables

Up to 8 columns and 50 rows; one cell per column, as text or a number. Right-align (`trailing`)
numeric columns. For more rows, summarise and offer a file instead.
