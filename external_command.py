import sublime, sublime_plugin, subprocess, _thread, re, os, io

HISTORY_SIZE = 16
HISTORY_FILENAME = '~/.sublimeexternalcommand_history'

class History:
    '''
    Command history management.
    '''
    def __init__(self, max_size, filename):
        self.max_size = max_size
        self.filename = os.path.expanduser(filename)

    def read(self):
        '''Read all commands from the history file and return as a list.'''
        commands = []
        if os.path.isfile(self.filename):
            f = io.open(self.filename, 'r', encoding='utf-8')
            commands = list(map(lambda s: s.strip(), f.readlines()))
            f.close()
        return commands

    def size(self):
        '''Return the number of commands in the history file.'''
        return len(self.read())

    def write(self, commands):
        '''Write commands to the history file.'''
        if len(commands) > self.max_size:
            commands = commands[0:self.max_size]

        f = io.open(self.filename, 'w', encoding='utf-8')
        f.write('\n'.join(commands))
        f.close()

    def add(self, command):
        '''Add a command to the history file.'''
        command = command.strip()
        # If the same command is in the history, remove it first.
        commands = list(filter(lambda s: s != command, self.read()))
        commands.insert(0, command)
        self.write(commands)

history = History(HISTORY_SIZE, HISTORY_FILENAME)

class SublimeExternalCommandHistory(sublime_plugin.TextCommand):
    '''
    Handles the UP/DOWN keys in the command input box.
    '''
    index = 0

    def run(self, edit, backwards=False):
        # sublime.status_message('History %d -> %s [%d]' % (SublimeExternalCommandHistory.index, backwards, history.size()))
        if backwards:
            SublimeExternalCommandHistory.index = min(SublimeExternalCommandHistory.index + 1, history.size() - 1)
        else:
            SublimeExternalCommandHistory.index = max(SublimeExternalCommandHistory.index - 1, -1)

        view = self.view
        view.erase(edit, sublime.Region(0, view.size()))
        # index == -1 means "current".  Just clear the input box.
        if SublimeExternalCommandHistory.index >= 0:
            command = history.read()[SublimeExternalCommandHistory.index]
            view.insert(edit, 0, command)

class CancelledException(Exception):
    pass

class CommandResult:
    def __init__(self, stdout, stderr, returncode):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def output(self):
        return self.stdout

    def error_message(self):
        if len(self.stderr) == 0:
            return "Shell returned %d" % self.returncode
        else:
            return "Shell returned %d:\n%s" % (self.returncode, self.stderr)

class ExternalCommandTask:
    def __init__(self, view, cmdline, on_done):
        self.view = view
        self.cmdline = cmdline
        self.cancelled = False
        self.done = False
        self.on_done = on_done
        self.proc = None

    def run_command(self, region_text):
        if self.cancelled:
            raise CancelledException()

        env = dict(os.environ)
        if not ('LC_CTYPE' in env or 'LC_ALL' in env or 'LANG' in env):
            env['LC_CTYPE'] = 'en_US.UTF-8'

        self.proc = subprocess.Popen(
            self.cmdline,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            universal_newlines=True,
            env=env)

        stdout, stderr = self.proc.communicate(region_text)
        returncode = self.proc.returncode

        return CommandResult(stdout, stderr, returncode)

    def show_error_panel(self, failed_results):
        panel = self.view.window().create_output_panel('external_command_errors')
        panel.set_read_only(False)
        for result in failed_results:
            panel.run_command('insert', {'characters': result.error_message()})

        panel.set_read_only(True)
        self.view.window().run_command('show_panel', {'panel': 'output.external_command_errors'})

    def handle_results(self, results):
        raise NotImplementedError()

    def task_input(self):
        raise NotImplementedError()

    def start(self):
        input_strings = self.task_input()

        def run():
            try:
                command_results = [self.run_command(string) for string in input_strings]
                if not self.cancelled:
                    self.handle_results([result.output() for result in command_results])

                    # handle errors
                    failed_results = [result for result in command_results if result.returncode]
                    if len(failed_results) > 0:
                        self.show_error_panel(failed_results)
            finally:
                self.done = True
                self.on_done(self)

        def spin(size, i=0, addend=1):
            if self.done or self.cancelled:
                self.view.erase_status('external_command')
                return

            before = i % size
            after = (size - 1) - before
            self.view.set_status('external_command', '%s [%s=%s]' % (self.cmdline, ' ' * before, ' ' * after))
            if not after:
                addend = -1
            if not before:
                addend = 1
            i += addend
            sublime.set_timeout(lambda: spin(size, i, addend), 100)

        _thread.start_new_thread(run, ())
        spin(8)

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass

class ReplaceTask(ExternalCommandTask):
    def __init__(self, *args, full_line=False, **kwargs):
        self.full_line = full_line
        super().__init__(*args, **kwargs)

    def task_input(self):
        # grab all the non-empty selections, but if there are none, grab the entire view
        sel = self.view.sel()
        selections = [region for region in sel if not region.empty()]
        if len(selections) == 0:
            self.regions = [sublime.Region(0, self.view.size())]
            if len(sel) >= 1:
                self.restore_pos = sel[0].a
            else:
                self.restore_pos = None
            self.restore_viewport = self.view.viewport_position()
        else:
            self.regions = selections
            self.restore_pos = None

        if self.full_line:
            self.regions = [self.view.full_line(region) for region in self.regions]

        return [self.view.substr(region) for region in self.regions]

    def handle_results(self, results):
        replace_regions(self.view, self.regions, results, self.restore_pos)
        # restore_pos = self.restore_pos
        # if restore_pos is not None:
            # self.view.sel().clear()
            # self.view.sel().add(sublime.Region(restore_pos))
            # (row, _) = self.view.rowcol(restore_pos)
            # self.view.show(self.view.text_point(row, 0))
        restore_viewport = self.restore_viewport
        if restore_viewport is not None:
            self.view.set_viewport_position(restore_viewport, False)
        # if restore_pos is not None:
        #     self.view.sel().clear()
        #     self.view.sel().add(sublime.Region(restore_pos))
        #     self.view.show_at_center(restore_pos)


class InsertTask(ExternalCommandTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def task_input(self):
        self.regions = [sublime.Region(region.begin(), region.end()) for region in self.view.sel()]
        return ['' for _ in self.regions]

    def handle_results(self, results):
        replace_regions(self.view, self.regions, results)

# Helper command for putting the output of the external command back into the buffer. The name
# was picked for the undu menu, not because it describes what this class does.
class RunExternalCommandCommand(sublime_plugin.TextCommand):
    def run(self, edit, regions, results, restore_pos):
        delta = 0
        for region, result in zip(regions, results):
            new_region = sublime.Region(region[0] + delta, region[1] + delta)
            # self.view.erase(edit, new_region)
            # delta += self.view.insert(edit, new_region.begin(), result) - new_region.size()
            self.view.replace(edit, new_region, result)
        if restore_pos is not None:
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(restore_pos))
            # (row, _) = self.view.rowcol(restore_pos)
            # self.view.show(self.view.text_point(row, 0))
            # self.view.show_at_center(restore_pos)

    def is_visible(self):
        return False

def replace_regions(view, regions, results, restore_pos):
    view.run_command('run_external_command', {'regions': [(region.begin(), region.end()) for region in regions], 'results': results, 'restore_pos': restore_pos})

class ExternalCommandManager(sublime_plugin.EventListener):
    tasks = {} # indexed by buffer id, so there can only be one task per buffer at a time

    def on_modified(self, view):
        task = self.task_for_view(view)
        if task:
            task.cancel()

    def on_selection_modified(self, view):
        task = self.task_for_view(view)
        if task and task.view.id() == view.id():
            task.cancel()

    def on_close(self, view):
        task = self.task_for_view(view)
        if task and task.view.id() == view.id():
            task.cancel()

    def task_for_view(self, view):
        return self.tasks.get(view.buffer_id())

    def start_task(self, sublime_command, cmdline, **kwargs):
        view = sublime_command.view
        def on_done(task):
            if self.task_for_view(view) == task:
                del self.tasks[view.buffer_id()]

        task = sublime_command.task_class(view, cmdline, on_done, **kwargs)
        self.tasks[view.buffer_id()] = task
        task.start()

    def __del__(self):
        for task in self.tasks.values():
            task.cancel()

class ExternalCommandBase(sublime_plugin.TextCommand):
    command_manager = ExternalCommandManager()

    def get_task(self):
        return self.command_manager.task_for_view(self.view)

    def is_enabled(self):
        if self.view.is_read_only():
            return False
        else:
            task = self.get_task()
            return task is None or type(task) == self.task_class

    def description(self):
        task = self.get_task()
        if task and type(task) == self.task_class:
            return 'Cancel External Command'
        else:
            return super().description()

    def run(self, edit, cmdline=None, **kwargs):
        task = self.get_task()
        if task and type(task) == self.task_class:
            task.cancel()
        else:
            def start(cmdline):
                if cmdline:
                    history.add(cmdline)
                    self.command_manager.start_task(self, cmdline, **kwargs)

            if cmdline is not None:
                start(cmdline)
            else:
                SublimeExternalCommandHistory.index = -1
                panel = self.view.window().show_input_panel('Command:', "", start, None, None)
                # This sets a scope to the input panel, which enables the history navigation commands.
                panel.set_syntax_file('Packages/SublimeExternalCommand/external_command.hidden-tmLanguage')
                panel.settings().set('is_widget', True)
                panel.settings().set('gutter', False)
                panel.settings().set('rulers', [])

class FilterThroughCommandCommand(ExternalCommandBase):
    task_class = ReplaceTask

class InsertCommandOutputCommand(ExternalCommandBase):
    task_class = InsertTask
