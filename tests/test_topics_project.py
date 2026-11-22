from _load import load
topics = load("topics", "topics.py")

def item(key, project, folder): return {"key": key, "project": project, "folder": folder, "state": "working", "updated": 1, "title": key, "tool": "claude", "where": ""}

def test_one_topic_per_project():
    real = topics.items, topics.load_labels
    try:
        topics.items = lambda now=None: [item("a", "", "Birch"), item("b", "", "Birch"), item("c", "", ""), item("d", "Kepler", "Kepler")]
        topics.load_labels = lambda: {"a": {"topic": "Birch Website", "at": 1}, "b": {"topic": "Kepler", "at": 2}, "c": {"topic": "Notes", "at": 3}, "d": {"topic": "Kepler", "at": 4}}
        got = {g["topic"]: sorted(i["key"] for i in g["items"]) for g in topics.overview()}
    finally: topics.items, topics.load_labels = real
    assert got == {"Birch Website": ["a", "b"], "Notes": ["c"], "Kepler": ["d"]}, got

if __name__ == "__main__": test_one_topic_per_project(); print("ok")
