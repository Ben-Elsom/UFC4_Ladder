"""Builds a pretend group chat for testing: 3 weeks of results plus awkward messages."""
import json, random, sys
random.seed(4)
H = 3600_000
start = 1_760_000_000_000
members = [("a1","JonoTheGoat"),("a2","Sam_K"),("a3","Temp48213"),("a4","Samurai_77"),("a5","xX_Reaper_Xx"),("a6","BenE_UFC")]
skill = {"a1":1.0,"a2":0.4,"a3":0.8,"a4":0.1,"a5":0.6,"a6":0.5}
short = {"a1":["jono","jonno","goat"],"a2":["sam","sam k","sammy"],"a3":["temp","temp48","tmep"],"a4":["samurai","smaurai","samu"],"a5":["reaper","reeper"],"a6":["ben","bene"]}
msgs = [{"from":"a2","body":"anyone on tonight?","ts":start}]
t = start + H
for i in range(70):
    a, b = random.sample([m[0] for m in members], 2)
    mode = random.choice(["mma","mma","sb"])
    p = 0.5 + (skill[a]-skill[b])*0.45
    wins = [random.random() < p for _ in range(3)]
    aw = sum(wins[:2]) + (wins[2] if sum(wins[:2])==1 else 0); bw = (2 if aw<2 else (1 if sum(wins[:2])==1 else 0))
    bo3 = random.random() < 0.6
    if bo3:
        aw, bw = (2, bw) if aw>=2 else (aw, 2)
    winner, loser = (a,b) if (aw>bw if bo3 else wins[0]) else (b,a)
    sc = f" {max(aw,bw)}-{min(aw,bw)}" if bo3 else ""
    md = random.choice({"mma":["mma","MMA","mma"],"sb":["sb","stand and bang","s&b","Stand n bang"]}[mode])
    reporter = random.choice([winner, loser])
    opp = loser if reporter == winner else winner
    nm = random.choice(short[opp])
    if reporter == winner:
        body = random.choice([f"beat {nm}{sc} {md}", f"w vs {nm} {md}{sc}", f"just beat {nm}{sc} {md}", f"{nm} got cooked{sc} {md}"])
    else:
        body = random.choice([f"lost to {nm}{sc} {md}", f"gg {nm}, lost{sc} {md}", f"{nm} beat me{sc} {md}", f"got smashed by {nm}{sc} {md}"])
    msgs.append({"from":reporter,"body":body,"ts":t})
    if random.random() < 0.25:  # opponent also posts it
        msgs.append({"from":opp,"body":f"lost to {random.choice(short[reporter])}{sc} {md}" if reporter==winner else f"beat {random.choice(short[reporter])}{sc} {md}","ts":t+20*60000})
    if random.random() < 0.3:
        msgs.append({"from":random.choice(members)[0],"body":random.choice(["ggs","I'd beat sam easily","who wants a game?","that beat was fire","jono smashed it lol","rematch?"]),"ts":t+5*60000})
    t += random.randint(2, 9) * H
# awkward cases at the end
msgs += [
  {"from":"a6","body":"beat temp 2-0","ts":t+1*H},            # no mode: asks (unless they played recently)
  {"from":"a6","body":"mma","ts":t+1*H+60000},                  # answers the question
  {"from":"a2","body":"beat zzqx 2-1 mma","ts":t+2*H},          # unknown name -> flagged
  {"from":"a5","body":"!board","ts":t+3*H},
  {"from":"a2","body":"!rank","ts":t+3*H+1000},
  {"from":"a3","body":"!h2h jono mma","ts":t+3*H+2000},
  {"from":"a4","body":"!belt","ts":t+3*H+3000},
]
json.dump({"bot":{"account_id":"bot","online_id":"LadderBot_UFC"},"group_name":"UFC 4 Killers","members":[{"account_id":a,"online_id":n} for a,n in members],"messages":msgs}, open(sys.argv[1],"w"), indent=1)
print(len(msgs), "messages, last ts", t+3*H+3000)
