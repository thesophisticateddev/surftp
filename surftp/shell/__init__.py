"""The interactive SSH shell alongside SFTP.

A shell is an extra channel on an already-authenticated ``SSHSession``, with
the terminal emulator (``pyte``) running in a child process so a runaway
command cannot freeze the UI.

This ``__init__`` is deliberately empty of imports: it runs *inside the child
process* before ``emulator.main`` starts, and it must not pull in ``asyncssh``
or ``textual`` or the whole import cost will be paid per shell. Import from
``surftp.shell.session`` / ``surftp.shell.emulator`` explicitly instead.
"""