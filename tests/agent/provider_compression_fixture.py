"""Real ContextCompressor route against the loopback hold oracle."""
import copy
import json
import os

from agent.context_compressor import ContextCompressor
from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.provider_control import HeldProvider


def main():
    compressor = ContextCompressor(model='claude-sonnet-4-5', provider='anthropic',
        base_url=os.environ['ANTHROPIC_BASE_URL'], api_key=os.environ['ANTHROPIC_API_KEY'],
        config_context_length=8192, protect_first_n=1, protect_last_n=3,
        quiet_mode=True, abort_on_summary_failure=True)
    messages = [{'role':'system','content':'Synthetic compression fixture'}]
    for i in range(30):
        messages.extend([{'role':'user','content':f'Historical user {i} '+('x '*600)},
                         {'role':'assistant','content':f'Historical answer {i} '+('y '*600)}])
    messages.append({'role':'user','content':os.environ['FIXTURE_QUERY']})
    before = copy.deepcopy(messages)
    try:
        compressor.compress(messages, current_tokens=20000, force=True)
    except (HeldProvider, AuxiliaryExplicitCancellation):
        assert messages == before
        assert not compressor._previous_summary
        print(json.dumps({'input_unchanged':True,
                          'reason':'Provider held; explicit operator resume required'}))
    else:
        raise AssertionError('compression did not propagate provider cancellation')


if __name__ == '__main__':
    main()
