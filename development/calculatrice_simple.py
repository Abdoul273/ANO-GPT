expression = input("Entrez une expression mathématique (ex: 2+2) : ")
try:
    resultat = eval(expression)
    print(f"Résultat : {resultat}")
except Exception as e:
    print(f"Erreur : {e}")