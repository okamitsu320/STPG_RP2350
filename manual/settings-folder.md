# Settings folder

`stpg-view` keeps **bindings** in one folder on this computer. A binding says which device is which: its port and USB
identity. Bindings are never written into a project or a bundle, so a project stays portable.

```text
<settings folder>/
  devices/       STPG boards          (managed in the Runner: Devices…)
  controllers/   GPIO controllers     (managed in the Runner: Controllers…)
  analyzers/     logic analyzers      (managed by the logic-analyzer tool; read only in the Runner)
```

## Which folder is used

The first of these that applies, decided once when the program starts:

1. `--config-dir FOLDER` on the command line.
2. The environment variable `STPG_CONFIG_DIR`, if it is not blank.
3. The default for your system:

| System | Default folder |
| --- | --- |
| Windows | `%APPDATA%\stpg` |
| macOS | `~/Library/Application Support/stpg` |
| Linux and others | `$XDG_CONFIG_HOME/stpg`, or `~/.config/stpg` when that variable is not set |

`stpg-view` and the logic-analyzer tool `stpg-la` use exactly this rule, so with the same options and environment
they use the same folder. Each prints the folder when it starts and shows it on its page, with the reason:

```text
stpg-view: settings folder: <your home>/.config/stpg (the per-user default; new: created when the first binding is saved)
```

## How values are read

| Value | What happens |
| --- | --- |
| `--config-dir ""` or only spaces | The program stops with an error. It does not fall back to another folder. |
| `STPG_CONFIG_DIR` empty | Counts as not set. |
| `STPG_CONFIG_DIR` only spaces | Counts as not set, and the start-up line says so. |
| Spaces in a path | Kept exactly as typed. Quote the value for your shell, not inside the value. |
| A value wrapped in quotes, such as `"C:\my settings"` stored in the variable with the quotes | The program stops with an error. Set the variable without the quotes. |
| `~` or `~/folder` | Your home folder. |
| `~name` that cannot be expanded | The program stops with an error. |
| A relative path | Taken from the folder the program was started in. Two programs started in different folders get different results, so prefer a full path. |
| A symbolic link | Followed at start. The folder shown is the real one and stays fixed while the program runs. |
| `XDG_CONFIG_HOME` that is not a full path | Ignored, and the start-up line says so. |

The folder is fixed at start and does not change while the program runs. To use another one, stop and restart.

## A new computer

Nothing is needed. The folder is created when you save the first binding; starting a program creates nothing.

## Using settings from an earlier setup

Point both programs at that folder, or copy the files yourself:

```sh
stpg-view --config-dir "/path/to/earlier/settings"
stpg-la   --config-dir "/path/to/earlier/settings"
```

No program moves, copies, converts or deletes settings for you. A file that cannot be read, or that has another or an
older format, is listed with the reason and left as it is; the other bindings still work. A binding is never replaced
by another device automatically. Saving over a binding with the same name needs **Replace** to be ticked.

If the folder cannot be used, for example because a file has that name or you lack permission, the page shows
**Settings folder problem** and the reason. No other folder is used instead.

Bindings contain USB serial numbers. Review them before sharing a settings folder.
