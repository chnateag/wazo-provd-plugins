{% extends 'base.tpl' %}

{% block model_specific_config %}
    {% if XX_mpk -%}
      {% for code, value in XX_mpk -%}
        <{{ code }}>{{ value }}</{{ code }}>
      {% endfor -%}
    {% endif -%}
{% endblock %}