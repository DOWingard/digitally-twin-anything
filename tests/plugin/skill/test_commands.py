"""Every `dta` command the skill names exists, and every option belongs to its command."""

from __future__ import annotations

from skilldocs import commands, parse_command


def test_the_skill_names_commands() -> None:
    assert len(commands()) > 50


def test_every_command_and_option_exists() -> None:
    problems = []
    for command in commands():
        parsed = parse_command(command.text)
        problems += [f"{command.where()}: `{command.text}`: {p}" for p in parsed.problems]
    assert not problems, "\n".join(problems)


def test_the_parser_refuses_what_the_tool_lacks() -> None:
    assert parse_command("dta manifest validate --complete").problems == []
    assert parse_command("dta manifest validat").problems
    assert parse_command("dta build --form load").problems
    assert parse_command("dta --json -w <workspace> <command>").generic
