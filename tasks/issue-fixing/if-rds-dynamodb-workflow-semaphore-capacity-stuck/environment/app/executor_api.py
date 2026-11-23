"""Client for the external executor's node-receipt API.

This module publishes executor evidence; controller lifecycle decisions are in
controller.py. Receipts use the cluster's existing DynamoDB Table resource.
"""
from botocore.exceptions import ClientError


def register_child(table, parent, child):
    """Record an immutable edge before issuing a child launch request.

    A worker retry may publish after its own payload has finished. Membership
    is append-only; a closed parent rejects any further publication.
    """
    try:
        table.update_item(
            Key={'wf_key': '@run/' + parent},
            UpdateExpression='ADD #children :child',
            ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND #started=:yes',
            ExpressionAttributeNames={'#children': 'children', '#closed': 'closed', '#started': 'started'},
            ExpressionAttributeValues={':child': {child}, ':no': False, ':yes': True})
        return True
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def start_node(table, node):
    """Delivery's only interlock is this node's receipt, not its ancestors."""
    try:
        table.update_item(
            Key={'wf_key': '@run/' + node}, UpdateExpression='SET #started=:yes',
            ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND attribute_not_exists(#started)',
            ExpressionAttributeNames={'#closed': 'closed', '#started': 'started'},
            ExpressionAttributeValues={':no': False, ':yes': True})
        return True
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


def finish_node(table, node):
    """Finish only this payload. This does not certify descendant completion."""
    table.update_item(Key={'wf_key': '@run/' + node}, UpdateExpression='SET #done=:yes',
                      ExpressionAttributeNames={'#done': 'done'}, ExpressionAttributeValues={':yes': True})
