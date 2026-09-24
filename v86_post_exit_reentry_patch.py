from pathlib import Path
import sys

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')

old="""                blocked = None
                if previous and previous["idea_id"] == signal.idea_id:
                    matching = [e for e in signal.confirmations if e.closed is True and e.direction==signal.direction
                                and e.information_id==signal.reentry_information_id
                                and e.completed_at>utc(previous["closed_at"]) and e.known_at<=at]
                    if signal.reentry_of_episode != previous["episode_id"] or not matching:
                        blocked = "REENTRY_REQUIRES_NEW_VALIDATED_INFORMATION"
"""
new="""                blocked = None
                if previous:
                    prev_closed=utc(previous["closed_at"])
                    prev_payload=json.loads(previous["payload"] or "{}")
                    prev_pos=prev_payload.get("position") if isinstance(prev_payload.get("position"),dict) else {}
                    prev_out=prev_payload.get("outcome") if isinstance(prev_payload.get("outcome"),dict) else {}
                    prev_dir=str(prev_pos.get("direction") or "")
                    same_direction=(prev_dir==signal.direction.value)
                    age_s=(at-prev_closed).total_seconds()
                    fresh_confirmations=[
                        e for e in signal.confirmations
                        if e.closed is True and e.direction==signal.direction
                        and e.completed_at>prev_closed and e.known_at<=at
                    ]
                    if previous["idea_id"] == signal.idea_id:
                        matching=[e for e in fresh_confirmations
                                  if e.information_id==signal.reentry_information_id]
                        if signal.reentry_of_episode != previous["episode_id"] or not matching:
                            blocked = "REENTRY_REQUIRES_NEW_VALIDATED_INFORMATION"
                    elif same_direction and 0 <= age_s < 900:
                        # A new idea_id alone is not new market information. After a recent close,
                        # the same-direction trade must earn its way back in.
                        prev_exit=None
                        try: prev_exit=decimal(prev_out.get("exit_price"),positive=True)
                        except Exception: prev_exit=None
                        extension=False
                        if prev_exit is not None and signal.movement_state=="ACCELERATION":
                            move=(quote.price/prev_exit-ONE)*signal.direction.sign
                            extension=bool(move>=D(".0040"))
                        if not fresh_confirmations and not extension:
                            blocked = "REENTRY_COOLDOWN_NO_NEW_INFORMATION"
"""
if old not in s: raise SystemExit('POST_EXIT_REENTRY_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')
print('V86_POST_EXIT_REENTRY_GUARD_ACTIVE')
