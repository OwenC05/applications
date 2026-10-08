"""The same optional, resumable 48-question bank as the legacy product."""
import json
from pathlib import Path

QUESTION_BANK = tuple(json.loads(Path(__file__).with_name('question_bank.json').read_text()))


def questions(sectors):
    return tuple(dict(item) for item in QUESTION_BANK
                 if item['sector'] == 'common' or item['sector'] in sectors)


def question(question_id, sectors):
    return next((item for item in questions(sectors) if item['id'] == question_id), None)
