// Aplica o filtro assim que uma opção é escolhida (sem JavaScript, aparece o botão "Filtrar").
document.querySelectorAll("form.filtros select").forEach(function (s) {
  s.addEventListener("change", function () {
    // não manda filtros vazios, para o endereço ficar limpo
    s.form.querySelectorAll("select").forEach(function (o) { o.disabled = !o.value; });
    s.form.submit();
  });
});
document.querySelectorAll(".sem-js").forEach(function (b) { b.hidden = true; });
// Em telas largas os filtros ficam sempre abertos; no celular, recolhidos até tocar em "Filtrar".
if (window.matchMedia("(min-width: 700px)").matches) {
  document.querySelectorAll("details.painel-filtros").forEach(function (d) { d.open = true; });
}
