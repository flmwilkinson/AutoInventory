"""Import-time side effects: if the scanner ever executes this, a canary
file appears and the no-execution test fails."""

with open("canary.txt", "w") as fh:
    fh.write("scanner executed scanned code\n")

print("evil module imported")


def harmless() -> str:
    return "nothing to see"
