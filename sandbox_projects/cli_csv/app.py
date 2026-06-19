import argparse
import csv


def total_amount(path):
    total = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += int(row["amount"])
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    print(f"total={total_amount(args.input)}")


if __name__ == "__main__":
    main()
