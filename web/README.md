# Dish Roulette

From the repository root, run:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory web
```

Open http://127.0.0.1:8000. The app uses plain HTML, CSS, and JavaScript and loads the fixture over HTTP.

With the default $12 budget and Boston University Central selected, click **Spin** and then **Reroll**. The dish should change. Choose **Vegan** at the same station to try rerolling into a dish on another line. Set the budget to $1 to see the no-match message.

The picker keeps the budget and every selected dietary need fixed. It prefers the starting line, then the most craving matches. Within that top group, a dish at the starting station gets three times the selection weight. Cheap means $8 or less. The app reports any starting-line or craving preferences it relaxes. If only the previous result qualifies, reroll shows a message and retains the card.

The fixture is fictional. The line, stop, and walk describe the destination. Transfer routes and live service are outside the demo. T5 can replace the fixture-backed picker with the backend endpoint.
