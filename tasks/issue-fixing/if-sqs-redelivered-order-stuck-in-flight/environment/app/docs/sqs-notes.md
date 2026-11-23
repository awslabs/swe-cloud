# SQS notes (AWS documentation excerpts)

From the Amazon SQS API Reference and Developer Guide. Wording is AWS's.

## DeleteMessage

> Each time you receive a message, meaning when a consumer retrieves a message from the queue, it
> comes with a unique ReceiptHandle. If you receive the same message more than once, you will get a
> different ReceiptHandle each time. When you want to delete a message using the DeleteMessage
> action, you must use the ReceiptHandle from the most recent time you received the message. If you
> use an old ReceiptHandle, the request will succeed, but the message might not be deleted.

> For standard queues, it is possible to receive a message even after you delete it. [...] You
> should ensure that your application is idempotent, so that receiving a message more than once
> does not cause issues.

## Visibility timeout

> The visibility timeout starts as soon as a message is delivered to you. During this period,
> you're expected to process and delete the message. If you don't delete it before the timeout
> expires, the message becomes visible again in the queue and can be retrieved by another consumer.

> In both standard and FIFO (First-In-First-Out) queues, the visibility timeout helps prevent
> multiple consumers from processing the same message simultaneously. However, due to the
> at-least-once delivery model of Amazon SQS, there's no absolute guarantee that a message won't be
> delivered more than once during the visibility timeout period.

> Terminating the timeout – If you decide not to process a received message, terminate its
> visibility timeout by setting the VisibilityTimeout to 0 seconds through the
> ChangeMessageVisibility action. This immediately makes the message available for other consumers
> to process.

> Implement a heartbeat mechanism to periodically extend the visibility timeout, ensuring the
> message remains invisible until processing is complete.

> Keep in mind that the visibility timeout has a maximum limit of 12 hours from when the message is
> first received. Extending the timeout doesn't reset this 12-hour limit.

## ChangeMessageVisibility

> Unlike with a queue, when you change the visibility timeout for a specific message the timeout
> value is applied immediately but isn't saved in memory for that message. If you don't delete a
> message after it is received, the visibility timeout for the message reverts to the original
> timeout value (not to the value you set using the ChangeMessageVisibility action) the next time
> the message is received.

## Dead-letter queues

> Use a redrive policy to specify the maxReceiveCount. The maxReceiveCount is the number of times a
> consumer can receive a message from a source queue before it is moved to a dead-letter queue.
