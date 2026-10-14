"""Build the flat context bundle (diff + relevant source files) for the
frontier-model baseline test on TS-H-01. Not wired into any production
path -- a one-off research script."""

from pathlib import Path

REPO_ROOT = Path("/Users/ksnaik/sample_repos/domain-driven-hexagon")
FILES = """
src/libs/ddd/aggregate-root.base.ts
src/libs/ddd/command.base.ts
src/libs/ddd/domain-event.base.ts
src/libs/ddd/entity.base.ts
src/libs/ddd/index.ts
src/libs/ddd/mapper.interface.ts
src/libs/ddd/query.base.ts
src/libs/ddd/repository.port.ts
src/libs/ddd/value-object.base.ts
src/modules/user/commands/create-user/create-user.cli.controller.ts
src/modules/user/commands/create-user/create-user.command.ts
src/modules/user/commands/create-user/create-user.http.controller.ts
src/modules/user/commands/create-user/create-user.message.controller.ts
src/modules/user/commands/create-user/create-user.request.dto.ts
src/modules/user/commands/create-user/create-user.service.ts
src/modules/user/commands/create-user/graphql-example/create-user.graphql-resolver.ts
src/modules/user/commands/delete-user/delete-user.http-controller.ts
src/modules/user/commands/delete-user/delete-user.service.ts
src/modules/user/database/user.repository.port.ts
src/modules/user/database/user.repository.ts
src/modules/user/domain/events/user-address-updated.domain-event.ts
src/modules/user/domain/events/user-created.domain-event.ts
src/modules/user/domain/events/user-deleted.domain-event.ts
src/modules/user/domain/events/user-role-changed.domain-event.ts
src/modules/user/domain/user.entity.ts
src/modules/user/domain/user.errors.ts
src/modules/user/domain/user.types.ts
src/modules/user/domain/value-objects/address.value-object.ts
src/modules/user/queries/find-users/find-users.graphql-resolver.ts
src/modules/user/queries/find-users/find-users.http.controller.ts
src/modules/user/queries/find-users/find-users.query-handler.ts
src/modules/user/user.di-tokens.ts
src/modules/user/user.mapper.ts
src/modules/user/user.module.ts
""".strip().splitlines()


def build() -> str:
    import subprocess
    diff = subprocess.run(
        ["git", "diff", "master", "ts-h-01-validate-postal-code-format-by-country", "--",
         "src/modules/user/domain/value-objects/address.value-object.ts"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout

    parts = [
        "# Task\n\n"
        "Below is a code change (diff) to a real TypeScript/NestJS repository, "
        "followed by a bundle of source files from the same repository (the user "
        "module and shared domain-driven-design base classes).\n\n"
        "Trace the FULL affected system path: starting from the changed code, "
        "find every HTTP entrypoint (route) that this change could affect, and "
        "the complete call/dispatch chain connecting the changed code to each "
        "entrypoint -- including any framework-mediated or virtual-dispatch "
        "hops (e.g. decorator-based command/event dispatch, abstract method "
        "overrides), not just direct function calls.\n\n"
        "For EVERY hop in the chain, cite the exact file and the specific line "
        "or code construct that justifies that hop. Do not guess or "
        "paraphrase -- only claim a hop you can point to literal evidence for "
        "in the files given below. If you are not sure a hop is real, say so "
        "explicitly rather than asserting it.\n\n"
        "Only use the files provided below -- do not assume you know this "
        "repository from any other source.\n\n"
        "## The change\n\n```diff\n" + diff + "\n```\n\n"
        "## Repository files\n\n"
    ]
    for rel in FILES:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        parts.append(f"### {rel}\n\n```typescript\n{path.read_text(encoding='utf-8')}\n```\n\n")
    return "".join(parts)


if __name__ == "__main__":
    out = Path(__file__).parent / "context_bundle.md"
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
