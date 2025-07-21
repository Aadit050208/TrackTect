from flask import Flask, render_template, request
from backend_logic import run_all_agents_return_data

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    if request.method == "POST":
        urls = request.form["urls"]
        twitter_handle = request.form.get("twitter_handle", "").strip()
        youtube_url = request.form.get("youtube_url", "").strip()

        input_urls = [url.strip() for url in urls.split(",") if url.strip()]

        # Build manual fallback mappings
        twitter_fallback = {url: twitter_handle for url in input_urls if twitter_handle}
        youtube_fallback = {url: youtube_url for url in input_urls if youtube_url}

        result = run_all_agents_return_data(input_urls, twitter_fallback, youtube_fallback)

    return render_template("index.html", result=result)


if __name__ == "__main__":
    app.run(debug=True)
