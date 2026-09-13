"""Explicit synthetic supervisor evidence for persistence tests; no OS process."""

from uuid import uuid4

from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.unit_of_work import PostgresUnitOfWork


async def record_stopped_launch(factory, admission):
    proof = SubscriptionLaunchTerminalProof(
        launch_id=str(uuid4()),
        pid=12345,
        process_identity="synthetic-process-start",
        outcome="exited",
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    async with PostgresUnitOfWork(factory) as work:
        await work.subscription.launch_intent(
            admission.attempt.attempt_id, proof.launch_id, worker_identity=admission.lease.owner
        )
        await work.subscription.launch_started(
            admission.attempt.attempt_id,
            proof.launch_id,
            worker_identity=admission.lease.owner,
            pid=proof.pid,
            process_start_token=proof.process_identity,
        )
        await work.subscription.launch_finished(
            admission.attempt.attempt_id,
            proof.launch_id,
            worker_identity=admission.lease.owner,
            terminal=proof,
            uncertain=False,
        )
        await work.commit()
    return proof
