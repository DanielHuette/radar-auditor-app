"""Feedback after every measurement of an audit (decision A4 of the Vorhaben Radar Auditor).

Context: Every measuring route reports an event when it ends, and after scoring every check does. A
failure reaches Daniel at once on four ways: a Windows notice, one card per audit on the Leitstand
board, a JSON lines file of the audit and the table audit_events. A failed route of a critical stage
stops the audit. | Input: Route outcomes and check results. | Output: Events and notices.
"""
