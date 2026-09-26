"""Owner audit commands; advice markers are explicit, never inferred from model prose."""
import json

from .gate import Gate


def register_cli(ctx):
    def setup(parser):
        sub = parser.add_subparsers(dest="operation", required=True)
        sub.add_parser("metrics", help="Aggregate counts and unverified rate, not cases")
        advice = sub.add_parser("advice", help="Record Gurney advice and its single attempted cycle")
        advice.add_argument("--profile", required=True)
        advice.add_argument("--task", required=True)
        advice.add_argument("--marker", choices=["advised", "tried"], required=True)
        receipt = sub.add_parser("receipt-send", help="Send and record one source-bound Chat receipt")
        receipt.add_argument("--source-request-id", required=True)
        receipt.add_argument("--card", required=True)
        receipt.add_argument("--message-file", required=True)

    def handle(args):
        from hermes_constants import get_default_hermes_root
        path = ctx.get_config("db_path", str(get_default_hermes_root() / "state/completion-gate/gate.db"))
        gate = Gate({"db_path": path}, extract=lambda _: [])
        if args.operation == "receipt-send":
            from .receipts import receipt_send
            settings = {
                key: ctx.get_config(key, "") for key in (
                    "chat_source_channel", "telegram_destination", "telegram_session_id",
                    "state_db_path", "kanban_db_path",
                )
            }
            settings["db_path"] = path
            result = receipt_send(
                settings, source_request_id=args.source_request_id,
                card_id=args.card, message_file=args.message_file)
        elif args.operation == "advice":
            gate.mark_advice(profile=args.profile, task_id=args.task, marker=args.marker)
            result = {"recorded": args.marker, "profile": args.profile, "task_id": args.task}
        else:
            result = gate.metrics()
        print(json.dumps(result, sort_keys=True))
        return result

    ctx.register_cli_command("completion-gate", "Completion evidence audit", setup, handle)
