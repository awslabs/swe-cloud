import os
import time
import uuid
from aws import client, payload_key
from idempotency import idempotent
from seam import seam


@idempotent
def dispatch(event, ctx):
    seam('handler_entry', execution_id=ctx.execution_id, payload_key=payload_key(event))

    def effect():
        client().update_item(TableName=os.environ.get('EFFECTS_TABLE', 'effects'),
                             Key={'payload_key': {'S': payload_key(event)},
                                  'execution_id': {'S': ctx.execution_id}},
                             UpdateExpression='SET dispatched_ms = :ms ADD dispatch_count :one',
                             ExpressionAttributeValues={':ms': {'N': str(int(time.time() * 1000))},
                                                        ':one': {'N': '1'}})
        return {'dispatch': ctx.execution_id, 'delivery_count': len(set(event.get('dispatch_keys', []))) or 1}

    dispatch_result = ctx.step('dispatch', effect)
    ctx.wait(float(os.environ.get('WAREHOUSE_WAIT_S', '15')))
    receipt = ctx.step('receipt', lambda: {'order': event, **dispatch_result, 'receipt': 'issued'})
    client().update_item(TableName=os.environ.get('EFFECTS_TABLE', 'effects'),
                         Key={'payload_key': {'S': payload_key(event)},
                              'execution_id': {'S': ctx.execution_id}},
                         UpdateExpression='ADD receipt_render_count :one',
                         ExpressionAttributeValues={':one': {'N': '1'}})
    # Delivery metadata belongs to this callback return, not the checkpoint log.
    return {**receipt, 'delivery_id': str(uuid.uuid4())}
