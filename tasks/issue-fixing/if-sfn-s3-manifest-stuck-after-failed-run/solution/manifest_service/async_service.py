import base64
import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    Self,
    TypedDict,
)
from uuid import UUID

from manifest_service import aws
from manifest_service.config import config

log = logging.getLogger(__name__)

JSON = dict[str, Any]


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True, kw_only=True)
class Token:
    """
    What a client holds while a manifest is being generated: it names the
    Step Functions execution of one generation attempt.
    """
    #: Derived from the manifest key
    generation_id: UUID

    #: Which attempt at generating the manifest the execution is; also the
    #: number of attempts that came before it
    iteration: int

    #: How often the client has polled with this token so far
    request_index: int

    #: Seconds the client should wait before polling again
    retry_after: int

    @property
    def execution_id(self) -> tuple[UUID, int]:
        return self.generation_id, self.iteration

    def encode(self) -> str:
        packed = json.dumps([
            self.generation_id.hex,
            self.iteration,
            self.request_index,
            self.retry_after
        ], separators=(',', ':'))
        return base64.urlsafe_b64encode(packed.encode()).decode()

    @classmethod
    def decode(cls, token: str) -> Self:
        try:
            generation_id, iteration, request_index, retry_after = json.loads(base64.urlsafe_b64decode(token))
            return cls(generation_id=UUID(hex=generation_id),
                       iteration=int(iteration),
                       request_index=int(request_index),
                       retry_after=int(retry_after))
        except Exception as e:
            raise InvalidTokenError(token) from e

    @classmethod
    def first(cls, generation_id: UUID, iteration: int) -> Self:
        return cls(generation_id=generation_id,
                   iteration=iteration,
                   request_index=0,
                   retry_after=cls._next_retry_after(0))

    def next(self, *, retry_after: int | None = None) -> Self:
        if retry_after is None:
            retry_after = self._next_retry_after(self.request_index)
        return dataclasses.replace(self,
                                   retry_after=retry_after,
                                   request_index=self.request_index + 1)

    @classmethod
    def _next_retry_after(cls, request_index: int) -> int:
        delays = [1, 1, 4, 6, 10]
        try:
            return delays[request_index]
        except IndexError:
            return delays[-1]


class ExecutionResult(TypedDict):
    input: JSON
    output: JSON


class NoSuchGeneration(Exception):

    def __init__(self, token: Token):
        super().__init__(token)
        self.token = token


class GenerationFinished(Exception):

    def __init__(self, token: Token):
        super().__init__(token)
        self.token = token


class GenerationFailed(Exception):

    def __init__(self, *, status: str, error: str | None, cause: str | None):
        super().__init__(status, error, cause)
        self.status = status
        self.error = error
        self.cause = cause


class PreviousGenerationFailed(Exception):

    def __init__(self, token: Token):
        super().__init__(token)
        self.token = token


class InvalidGeneration(Exception):

    def __init__(self, token: Token):
        super().__init__(token)
        self.token = token


class GenerationService:
    """
    Starts manifest generation attempts and reports on their progress.
    """

    def start_generation(self,
                         generation_id: UUID,
                         input: JSON,
                         iteration: int
                         ) -> Token:
        execution_name = self.execution_name(generation_id, iteration)
        execution_arn = self.execution_arn(execution_name)
        # Only the input spells out the manifest key, the execution name is
        # just a hash of it, so log both together for whoever has to trace an
        # execution back to a request.
        log.info('Starting execution %r for input %r', execution_arn, input)
        token = Token.first(generation_id, iteration)
        try:
            # Should an execution by this name already be running, having been
            # started with the same input we are passing, Step Functions treats
            # this call as a repeat and returns that execution.
            #
            # Step Functions compares the input verbatim, so it is serialized
            # canonically: requests for the same manifest may carry the same
            # filters in a different order.
            execution = self._sfn.start_execution(stateMachineArn=self.machine_arn,
                                                  name=execution_name,
                                                  input=json.dumps(input, sort_keys=True))
        except self._sfn.exceptions.ExecutionAlreadyExists:
            # The name is taken: either the execution by that name is over, or
            # it was started with an input other than ours. We don't expect the
            # second case. Everything in the input that shapes the manifest
            # goes into the manifest key, and from there into the name, and the
            # rest of the input never varies between requests.
            #
            # So the execution is over, and the client gets its token. Polling
            # that token is what delivers the manifest. We could look the
            # manifest up right here instead, but that would add a second path
            # to the manifest for a case that rarely comes up.
            #
            # Whether the execution is over has to be read from its status: an
            # execution started with an equal input that was serialized
            # differently is still running, and is the attempt to join. A
            # failed execution, on the other hand, holds on to its name, and a
            # token for it would only lead the client back to the failure, so
            # the caller has to move on to the next iteration.
            execution = self._sfn.describe_execution(executionArn=execution_arn)
            if input != json.loads(execution['input']):
                raise InvalidGeneration(token)
            status = execution['status']
            if status == 'RUNNING':
                log.info('Joining running execution %r', execution_arn)
                return token
            elif status == 'SUCCEEDED':
                log.info('Execution %r has already ended', execution_arn)
                raise GenerationFinished(token)
            else:
                log.info('Execution %r ended with status %r', execution_arn, status)
                raise PreviousGenerationFailed(token)
        else:
            assert execution_arn == execution['executionArn'], (execution_arn, execution)
            log.info('Started execution %r or it was already running', execution_arn)
            return token

    def inspect_generation(self, token: Token) -> Token | ExecutionResult:
        execution_name = self.execution_name(token.generation_id, token.iteration)
        execution_arn = self.execution_arn(execution_name)
        try:
            execution = self._sfn.describe_execution(executionArn=execution_arn)
        except self._sfn.exceptions.ExecutionDoesNotExist:
            raise NoSuchGeneration(token)
        else:
            output = execution.get('output')
            status = execution['status']
            if status == 'SUCCEEDED':
                if output is None:
                    log.info('Execution %r succeeded, no output yet', execution_arn)
                    return token.next(retry_after=1)
                else:
                    log.info('Execution %r succeeded with output %r', execution_arn, output)
                    return {k: json.loads(execution[k]) for k in ['input', 'output']}
            elif status == 'RUNNING':
                log.info('Execution %r is still running', execution_arn)
                return token.next()
            else:
                raise GenerationFailed(status=status,
                                       error=execution.get('error'),
                                       cause=execution.get('cause'))

    def first_iteration(self, generation_id: UUID) -> int:
        """
        The iteration a request for the manifest can start at: every attempt
        before it has ended without leaving the manifest in the bucket.
        """
        try:
            response = self._s3.get_object(Bucket=config.manifest_bucket,
                                           Key=self._hint_key(generation_id))
        except self._s3.exceptions.NoSuchKey:
            return 0
        else:
            with response['Body'] as body:
                return int(json.load(body)['iteration'])

    def record_iteration(self, generation_id: UUID, iteration: int):
        """
        Remember that every attempt before `iteration` has ended. Concurrent
        requests may record their findings in any order. That is fine: a
        request that starts below the latest attempt only checks a few more
        names before it gets there.
        """
        self._s3.put_object(Bucket=config.manifest_bucket,
                            Key=self._hint_key(generation_id),
                            Body=json.dumps({'iteration': iteration}).encode(),
                            ContentType='application/json')

    def _hint_key(self, generation_id: UUID) -> str:
        return f'_generations/{generation_id}.json'

    @property
    def machine_arn(self) -> str:
        return config.manifest_state_machine_arn

    def execution_arn(self, execution_name: str) -> str:
        # arn:aws:states:<region>:<account>:stateMachine:<machine>
        prefix, machine_name = self.machine_arn.split(':stateMachine:')
        return f'{prefix}:execution:{machine_name}:{execution_name}'

    def execution_name(self, generation_id: UUID, iteration: int) -> str:
        assert isinstance(generation_id, UUID), generation_id
        assert isinstance(iteration, int), iteration
        execution_name = f'{generation_id}_{iteration}'
        assert 0 < len(execution_name) <= 80, execution_name
        return execution_name

    @property
    def _sfn(self):
        return aws.client('stepfunctions')

    @property
    def _s3(self):
        return aws.client('s3')
