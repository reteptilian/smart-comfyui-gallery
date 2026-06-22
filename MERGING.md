Keep `main` clean and tracking upstream, while maintaining your customized version on `feature/remotebackends`—or rename it to something permanent like `custom/remotebackends`.

To commit new work:

```bash
git switch feature/remotebackends
git add .
git commit -m <USEFUL COMMENT>
git push -u origin feature/remotebackends
```

When upstream releases an update:

```bash
git fetch upstream

git switch main
git merge --ff-only upstream/main
git push origin main

git switch feature/remotebackends
git merge main

# Run tests after resolving any conflicts
uv run python -m unittest discover -s tests -v

git push origin feature/remotebackends
```

If conflicts occur:

```bash
git status
# Edit conflicted files
git add <resolved-files>
git commit
git push origin feature/remotebackends
```

I recommend merging, not rebasing, for this long-lived published branch. Merge commits provide a clear record of each upstream update.