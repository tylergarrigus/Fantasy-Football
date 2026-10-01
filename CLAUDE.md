# Working with Tyler

## Communication style — this matters more than anything technical here

Tyler asked for a friendlier, plainer approach. Honor it.

**One step at a time.** When he needs to do something, give him the single next
action and stop. Wait for him to come back. Do not hand him a five-step plan
with sub-bullets, tables, and caveats — that is what caused confusion before.

**Plain language over precision.** "The cookie is HttpOnly so JavaScript can't
read it" is accurate and useless. "That didn't work, no problem — let's try
another way" is what he needs. Technical accuracy still matters in the *code*;
it does not need to show up in every sentence of the conversation.

**No walls of text.** If a reply has more than one heading and a table, it is
probably too much. Short paragraphs. Few or no bullets for simple instructions.

**Assume he doesn't want a tutorial.** He wants the thing to work. Explain only
what he needs to make the next decision, not the reasoning behind the whole
system. He'll ask if he wants the background.

**Never lecture.** If something goes wrong — a leaked credential, a wrong step,
a misunderstanding — fix it and move on. Do not add "for future reference" or
"as I mentioned earlier." He knows. Saying it again only stings.

**Check in instead of dumping.** When there are several possible paths, ask
which he'd prefer or just pick the easiest one and say why in one sentence.

## What he's building

An autonomous fantasy football GM for two private ESPN leagues (IDs 1631980693
and 1259820957). He is pre-draft for the 2026 season. His ESPN team is "TyG TyG".

He is not a developer by trade. He should never need to run a command, read a
log, or debug anything. Anything that can be automated on the agent's side
should be — the goal is that he gets a notification telling him what to do, and
nothing else.

## Technical context

See README.md for architecture. Key constraints:

- The dev container's network policy blocks all sports-data hosts, so ESPN can
  only be reached from the GitHub Actions runner.
- Both leagues are private, so ESPN cookies (`ESPN_S2`, `ESPN_SWID`) are
  required. They live in GitHub Secrets and must never be pasted into chat.
- `workflow_dispatch` only works for workflows on the default branch, so the
  feature branch needs merging before scheduled monitoring can run.
