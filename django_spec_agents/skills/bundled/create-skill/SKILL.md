---
name: "create-skill"
description: "Help the user write a new skill: interview them about the task, draft its name, description and instructions, and save it with create_skill once they agree."
user-invocable: true
disable-model-invocation: false
---

# Create a skill with the user

A skill is a set of instructions saved under a name. It is offered to you by its name and
description, and its instructions load when it is used. It grants no tools and no
permissions: everything it asks for still goes through the same approvals as before.

## 1. Check what exists

Call `list_skills` first. If a skill already covers the task, say so and offer to change it
with `update_skill` instead of creating a second one. Only a skill marked `writable` can be
changed.

## 2. Interview the user

Ask short questions, one or two at a time, until you can answer each of these:

- What task should the skill handle, and what does a good result look like?
- When should it be used? What would the user say or ask that should bring it up?
- Which steps, in which order? What should be checked before and after?
- What should it never do, and when should it stop and ask?
- Should the user be able to pick it from their skill palette, should you be able to load
  it on your own, or both?

Do not invent requirements the user has not given; ask instead.

## 3. Draft it and show the draft

- **name**: kebab-case, lowercase letters and digits joined by single hyphens, at most 64
  characters, such as `weekly-report`.
- **description**: one or two sentences on what it does and when to use it. This is all
  that is shown until the skill is loaded, so make it specific.
- **body**: the instructions, written to the agent that will follow them, in Markdown, with
  the steps in order.

Show the user the full draft and ask for changes. Revise until they say it is right.

## 4. Save it

Call `create_skill` with the name, description and body, and with `user_invocable` and
`model_invocable` set as the user chose. The user approves the write after reading the body.
If the tool reports a problem with the draft, fix it and try again. If it reports that the
write was refused, tell the user: their account may not create skills here.
